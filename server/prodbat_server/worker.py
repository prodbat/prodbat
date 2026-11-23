"""Everything that happens on a timer. Run with `python -m prodbat_server worker`.

One process is enough; several can run at once because each piece of
work is claimed with row locks.

Beside the timed loop runs the job queue (queue.py), for work that is
asked for, such as checking a site's packages when the list changes.
Timers that find such work only enqueue it.
"""

import asyncio
import datetime as dt
import time

import psycopg
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from . import facts, queue, traffic
from .app import load_extensions
from .setup_emails import send_setup_emails
from .github import send_pending_github
from .push import send_pending_push
from .slack import send_pending_slack
from .telegram import register_webhook, send_pending_telegram
from .texts import send_pending_texts
from .webhooks import send_pending_webhooks
from .checks import make_client, refresh_expiry, run_due_checks
from .config import Settings
from .detect import evaluate_all
from .mailer import send_pending
from .weekly import send_weekly

# The job queue wakes on a notification, or after WAKE seconds for retries
# that were put off. Without notifications it looks every POLL seconds.
WAKE = 30
POLL = 5
PRUNE =["pageviews", "browser_errors", "failed_requests", "server_errors", "site_checks"]


async def prune(sessions, settings: Settings) -> None:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=settings.keep_days)
    async with sessions() as db:
        for table in PRUNE:
            # Each project keeps its own number of days, or the server's.
            await db.execute(text(f"""
                delete from {table} t using projects p
                where t.project_id = p.id and t.ts < now() - make_interval(days => coalesce(p.keep_days, :days))"""),
                {"days": settings.keep_days})
        await db.execute(text("delete from emails where sent_at < :cutoff"), {"cutoff": cutoff})
        await db.execute(text("delete from texts where sent_at < :cutoff"), {"cutoff": cutoff})
        await db.execute(text("delete from slack_posts where sent_at < :cutoff"), {"cutoff": cutoff})
        await db.execute(text("delete from push_messages where sent_at < :cutoff"), {"cutoff": cutoff})
        await db.execute(text("delete from telegram_messages where sent_at < :cutoff"), {"cutoff": cutoff})
        await db.execute(text("delete from github_posts where sent_at < :cutoff"), {"cutoff": cutoff})
        # Webhook deliveries are only kept to show the last few on the page.
        await db.execute(text("delete from webhook_deliveries where created_at < now() - interval '30 days'"))
        await db.execute(text("delete from queue_reports where ts < now() - interval '7 days'"))
        await queue.prune(db)
        await facts.prune(db)
        # Log lines have their own, shorter life; a project can have its own number of days.
        await db.execute(text("""
            delete from log_lines l using projects p
            where l.project_id = p.id and l.ts < now() - make_interval(days => coalesce(p.log_keep_days, :days))"""),
            {"days": settings.log_keep_days})
        await db.commit()


async def listen(settings: Settings):
    """A connection of its own that hears when a job is enqueued, or None if that fails."""
    try:
        url = make_url(settings.database_url).set(drivername="postgresql").render_as_string(hide_password=False)
        conn = await psycopg.AsyncConnection.connect(url, autocommit=True, connect_timeout=10)
        await conn.execute(f"listen {queue.CHANNEL}")
        return conn
    except Exception as exc:
        print(f"worker: not listening for jobs, checking every {POLL} seconds: {type(exc).__name__}: {exc}", flush=True)
        return None


async def queue_loop(sessions, client, settings: Settings) -> None:
    """Runs jobs as they are enqueued. Between rounds it waits for a
    notification, or WAKE seconds for jobs that were put off to retry."""
    conn = None
    try:
        while True:
            # Listening starts before the jobs are looked at, so one enqueued
            # in between still wakes the loop.
            if conn is None or conn.closed:
                conn = await listen(settings)
            try:
                await queue.run_due(sessions, client, settings)
            except Exception as exc:  # the database may be down; try again shortly
                print(f"worker: job queue: {type(exc).__name__}: {exc}", flush=True)
                await asyncio.sleep(POLL)
                continue
            if conn is None:
                await asyncio.sleep(POLL)
                continue
            try:
                # Notifications sent while jobs ran are waiting on the connection, so none is missed.
                async for _ in conn.notifies(timeout=WAKE, stop_after=1):
                    pass
            except Exception as exc:
                print(f"worker: stopped listening for jobs: {type(exc).__name__}: {exc}", flush=True)
                await conn.close()
                conn = None
                await asyncio.sleep(POLL)
    finally:
        if conn is not None:
            await conn.close()


async def worker(settings: Settings | None = None) -> None:
    settings = settings or Settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    last: dict[str, float] = {}

    def due(name: str, seconds: float) -> bool:
        if time.monotonic() - last.get(name, -1e9) < seconds:
            return False
        last[name] = time.monotonic()
        return True

    extensions = [m for m in load_extensions(settings) if hasattr(m, "tick")]

    async with make_client() as client:
        jobs = asyncio.create_task(queue_loop(sessions, client, settings))
        while True:
            if jobs.done():  # it never returns; if it somehow did, start it again
                jobs = asyncio.create_task(queue_loop(sessions, client, settings))
            try:
                await run_due_checks(sessions, client, settings)
                if due("evaluate", 60):
                    await evaluate_all(sessions, settings)
                    await send_setup_emails(sessions, settings)
                for module in extensions:
                    await module.tick(sessions, settings, client)
                if due("traffic", 600):
                    await traffic.roll_up_all(sessions)
                if due("slow", 900):
                    await refresh_expiry(sessions, client, settings)
                    await send_weekly(sessions, settings)
                if due("recheck", 3600):
                    await queue.enqueue_due_checks(sessions)
                if due("prune", 3600):
                    await prune(sessions, settings)
                await send_pending(sessions, settings)
                await send_pending_texts(sessions, settings, client)
                await send_pending_slack(sessions, settings, client)
                await send_pending_push(sessions, settings, client)
                await send_pending_telegram(sessions, settings, client)
                await send_pending_webhooks(sessions, settings, client)
                await send_pending_github(sessions, settings, client)
                if settings.telegram_configured and due("telegram-webhook", 6 * 3600):
                    # Where Telegram sends the bot's messages; repeating it is harmless.
                    try:
                        await register_webhook(client, settings)
                    except Exception as exc:
                        print(f"worker: Telegram webhook not set: {exc}", flush=True)
                if settings.watcher_url and due("watcher", 60):
                    await client.get(settings.watcher_url)
            except Exception as exc:  # one bad pass must not stop the watch
                print(f"worker: {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(15)
