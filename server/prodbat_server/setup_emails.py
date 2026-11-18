"""Two one-off emails around setup: "Prodbat is watching yourapp.com" when the
first visitor arrives, and "Prodbat hasn't heard from yourapp.com yet" if a
day passes with nothing at all. Each goes once, and only to a project with
an address to send to and monitoring switched on."""

import datetime as dt

from sqlalchemy import func, select

from .config import Settings
from .incidents import queue_email
from .models import Dependency, Project
from .words import clock, plural

NUDGE_AFTER = dt.timedelta(hours=24)


def live_email(project: Project, settings: Settings, packages: int) -> tuple[str, str]:
    page = f"{settings.base}/d/{project.page_token}"
    minutes = max(1, project.check_every_seconds // 60)
    watched = [
        f"Your site is loaded from outside every {'minute' if minutes == 1 else f'{minutes} minutes'}.",
        "A failure on checkout or sign-in emails you the first time it happens.",
    ]
    if packages:
        watched.append(f"{plural(packages, 'package')} are checked every day for security problems and end of support.")
    watched.append("Every Monday you'll get a short summary.")
    body = "\n".join([
        f"Your first visitor arrived at {clock(project.first_data_at, project.timezone)}, so Prodbat is live on {project.domain}.",
        "",
        "From now on:",
        *[f"  - {line}" for line in watched],
        "",
        "To see what an alert looks like and make sure it reaches your inbox, send yourself a test:",
        f"  {page}#test-alert",
        "",
        f"Your page: {page}",
    ])
    return f"Prodbat is watching {project.domain}", body


def nudge_email(project: Project, settings: Settings) -> tuple[str, str]:
    body = "\n".join([
        f"Prodbat was set up for {project.domain} yesterday but no data has arrived, so nothing is being "
        "watched yet.",
        "",
        "It's usually one of two things: the change wasn't deployed, or PRODBAT_KEY isn't set in your host's "
        "environment variables.",
        "",
        "Paste this into your coding agent and it will find out which:",
        "",
        "    Run npx prodbat verify and fix what it reports.",
        "",
        f"Your page: {settings.base}/d/{project.page_token}",
    ])
    return f"Prodbat hasn't heard from {project.domain} yet", body


async def send_setup_emails(sessions, settings: Settings, now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.UTC)
    queued = 0
    async with sessions() as db:
        ready = select(Project).where(Project.alert_email.is_not(None), Project.paused_at.is_(None))
        for project in (await db.scalars(ready.where(
                Project.first_data_at.is_not(None), Project.live_email_at.is_(None))
                .with_for_update(skip_locked=True))).all():
            packages = await db.scalar(select(func.count()).select_from(Dependency)
                                       .where(Dependency.project_id == project.id)) or 0
            queue_email(db, project, settings, *live_email(project, settings, packages))
            project.live_email_at = now
            queued += 1
        for project in (await db.scalars(ready.where(
                Project.first_data_at.is_(None), Project.first_server_at.is_(None),
                Project.nudge_email_at.is_(None), Project.created_at < now - NUDGE_AFTER)
                .with_for_update(skip_locked=True))).all():
            queue_email(db, project, settings, *nudge_email(project, settings))
            project.nudge_email_at = now
            queued += 1
        await db.commit()
    return queued
