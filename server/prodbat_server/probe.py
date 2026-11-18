"""A probe: loads home pages from another region and records what it saw.
It never alerts; the main worker reads its checks before it calls a site
down, so a problem on the network between one region and the site is not
mistaken for the site being down.

    PRODBAT_REGION=frankfurt python -m prodbat_server probe

Run one per region, against the same database as the server. Only projects
with check_regions switched on are probed."""

import asyncio
import datetime as dt
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .checks import check_site, make_client
from .config import Settings
from .models import Project, SiteCheck


async def probe_once(sessions, client, settings: Settings, last: dict) -> int:
    """Check every project that is due by its own interval; return how many were checked."""
    async with sessions() as db:
        projects = (await db.execute(select(Project.id, Project.domain, Project.check_every_seconds).where(
            Project.check_regions.is_(True), Project.paused_at.is_(None)))).all()
    due = [(pid, domain) for pid, domain, every in projects
           if time.monotonic() - last.get(pid, -1e9) >= (every or 300)]
    results = await asyncio.gather(*(check_site(client, domain) for _, domain in due))
    now = dt.datetime.now(dt.UTC)
    async with sessions() as db:
        for (pid, _), result in zip(due, results):
            last[pid] = time.monotonic()
            db.add(SiteCheck(project_id=pid, ts=now, path="/", ok=result.ok, status=result.status, ms=result.ms,
                             error=result.error, region=settings.region))
        await db.commit()
    return len(due)


async def probe(settings: Settings | None = None) -> None:
    settings = settings or Settings()
    if not settings.region:
        raise SystemExit("A probe needs a region name: set PRODBAT_REGION, such as frankfurt.")
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    last: dict = {}
    async with make_client() as client:
        while True:
            try:
                await probe_once(sessions, client, settings, last)
            except Exception as exc:  # one bad pass must not stop the probe
                print(f"probe ({settings.region}): {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(10)
