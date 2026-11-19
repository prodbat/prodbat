"""Work the worker runs soon after something asks for it, such as checking
a site's packages when the list changes.

A job is a row in job_queue. Enqueueing sends a Postgres notification on
commit, so a waiting worker starts on it within a second or two. Timers
never do the work themselves; they only enqueue it. Several workers can
run at once: a job is claimed with FOR UPDATE SKIP LOCKED.
"""

import datetime as dt
import uuid

import httpx
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .advisories import check_advisories
from .config import Settings
from .freshness import check_freshness
from .models import Project, QueuedJob

CHANNEL = "prodbat_jobs"
# A job locked longer than this is taken to belong to a worker that died.
LOCK_TIMEOUT = dt.timedelta(minutes=10)
# Wait after the 1st, 2nd and 3rd failure. The 4th is the last.
BACKOFF = [dt.timedelta(minutes=1), dt.timedelta(minutes=10), dt.timedelta(hours=1)]
MAX_ATTEMPTS = 4
RECHECK = dt.timedelta(days=1)


async def enqueue(db, kind: str, project_id: uuid.UUID | None = None, *, key: str | None = None,
                  run_after: dt.datetime | None = None) -> None:
    """Ask for `kind` to run for a project (or for `key`). If the same job
    is already waiting, it keeps its place, or moves up to `run_after` if
    that is sooner. The caller commits; the worker is told on commit."""
    key = key if key is not None else str(project_id or "")
    when = run_after or dt.datetime.now(dt.UTC)
    insert = pg_insert(QueuedJob).values(kind=kind, key=key, project_id=project_id, run_after=when)
    await db.execute(insert.on_conflict_do_update(
        index_elements=["kind", "key"], index_where=text("finished_at is null and locked_at is null"),
        set_={"run_after": text("least(job_queue.run_after, excluded.run_after)")}))
    await db.execute(text("select pg_notify(:channel, '')"), {"channel": CHANNEL})


# ---- what each kind of job does ----------------------------------------------

async def check_packages(sessions, client: httpx.AsyncClient, settings: Settings, job: QueuedJob) -> None:
    """Security problems first: an open one stops a deprecation alert for the same package."""
    now = dt.datetime.now(dt.UTC)
    async with sessions() as db:
        project = await db.get(Project, job.project_id)
        if project is None or project.paused_at is not None:
            return
        await check_advisories(db, project, client, settings, now)
        await db.commit()
        await check_freshness(db, project, client, settings, now)
        await db.commit()


HANDLERS = {"check_packages": check_packages}


async def enqueue_due_checks(sessions) -> int:
    """Once an hour: a package check for every site not checked in a day."""
    cutoff = dt.datetime.now(dt.UTC) - RECHECK
    async with sessions() as db:
        stale = (await db.scalars(select(Project.id).where(Project.paused_at.is_(None)).where(
            Project.advisories_checked_at.is_(None) | (Project.advisories_checked_at < cutoff)
            | Project.freshness_checked_at.is_(None) | (Project.freshness_checked_at < cutoff)))).all()
        for project_id in stale:
            await enqueue(db, "check_packages", project_id)
        await db.commit()
    return len(stale)


# ---- running them ----------------------------------------------------------

async def claim(db) -> QueuedJob | None:
    """The job that has waited longest and is due, locked for this worker."""
    job = await db.scalar(select(QueuedJob).where(
        QueuedJob.finished_at.is_(None), QueuedJob.run_after <= func.now(),
        or_(QueuedJob.locked_at.is_(None), QueuedJob.locked_at < func.now() - LOCK_TIMEOUT))
        .order_by(QueuedJob.run_after).limit(1).with_for_update(skip_locked=True))
    if job is None:
        return None
    job.locked_at = dt.datetime.now(dt.UTC)
    job.attempts += 1
    await db.commit()
    return job


async def run_one(sessions, client: httpx.AsyncClient, settings: Settings) -> bool:
    """Claim one due job and run it. False when there was nothing to do."""
    async with sessions() as db:
        job = await claim(db)
    if job is None:
        return False
    try:
        await HANDLERS[job.kind](sessions, client, settings, job)
    except Exception as exc:  # one job's error must not stop the others
        error = f"{type(exc).__name__}: {exc}"[:500]
        print(f"worker: job {job.id} ({job.kind} {job.key}), attempt {job.attempts}: {error}", flush=True)
        await failed(sessions, job, error)
    else:
        async with sessions() as db:
            await db.execute(update(QueuedJob).where(QueuedJob.id == job.id).values(finished_at=func.now()))
            await db.commit()
    return True


async def failed(sessions, job: QueuedJob, error: str) -> None:
    async with sessions() as db:
        values = {"last_error": error}
        # The same job was enqueued again while this one ran: that one will do the work.
        again = await db.scalar(select(QueuedJob.id).where(
            QueuedJob.kind == job.kind, QueuedJob.key == job.key, QueuedJob.id != job.id,
            QueuedJob.finished_at.is_(None), QueuedJob.locked_at.is_(None)))
        if again is not None or job.attempts >= MAX_ATTEMPTS:
            values |= {"finished_at": func.now(), "failed": again is None}
        else:
            values |= {"locked_at": None, "run_after": dt.datetime.now(dt.UTC) + BACKOFF[job.attempts - 1]}
        await db.execute(update(QueuedJob).where(QueuedJob.id == job.id).values(**values))
        await db.commit()


async def run_due(sessions, client: httpx.AsyncClient, settings: Settings) -> int:
    """Every job that is due, one after another."""
    done = 0
    while await run_one(sessions, client, settings):
        done += 1
    return done


async def prune(db) -> None:
    """Finished jobs are only kept to see what went wrong lately."""
    await db.execute(text("delete from job_queue where finished_at < now() - interval '7 days'"))
