"""Everything that happens on a timer. Run with `python -m prodbat_server worker`.

One process is enough; several can run at once because each piece of
work is claimed with row locks.
"""

import asyncio
import datetime as dt
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .advisories import refresh_advisories
from .app import load_extensions
from .setup_emails import send_setup_emails
from .push import send_pending_push
from .slack import send_pending_slack
from .telegram import register_webhook, send_pending_telegram
from .texts import send_pending_texts
from .checks import make_client, refresh_expiry, run_due_checks
from .config import Settings
from .detect import evaluate_all
from .freshness import refresh_freshness
from .mailer import send_pending
from .weekly import send_weekly

PRUNE = ["pageviews", "browser_errors", "failed_requests", "server_errors", "site_checks"]


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
        await db.execute(text("delete from queue_reports where ts < now() - interval '7 days'"))
        # Log lines have their own, shorter life; a project can have its own number of days.
        await db.execute(text("""
            delete from log_lines l using projects p
            where l.project_id = p.id and l.ts < now() - make_interval(days => coalesce(p.log_keep_days, :days))"""),
            {"days": settings.log_keep_days})
        await db.commit()


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
        while True:
            try:
                await run_due_checks(sessions, client, settings)
                if due("evaluate", 60):
                    await evaluate_all(sessions, settings)
                    await send_setup_emails(sessions, settings)
                for module in extensions:
                    await module.tick(sessions, settings, client)
                if due("slow", 900):
                    await refresh_expiry(sessions, client, settings)
                    await refresh_advisories(sessions, client, settings)
                    await refresh_freshness(sessions, client, settings)
                    await send_weekly(sessions, settings)
                if due("prune", 3600):
                    await prune(sessions, settings)
                await send_pending(sessions, settings)
                await send_pending_texts(sessions, settings, client)
                await send_pending_slack(sessions, settings, client)
                await send_pending_push(sessions, settings, client)
                await send_pending_telegram(sessions, settings, client)
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
