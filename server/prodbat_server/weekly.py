"""The Monday email. Its subject and first lines are the owner's own
numbers; "nothing broke" is one line among them. It never carries an
offer, and it comes from a different sender name than alerts so the two
cannot be confused."""

import datetime as dt

from sqlalchemy import select

from .config import Settings
from .incidents import queue_email
from .models import Incident, Project
from .summary import WEEK, Summary, gather, past_tense
from .words import local_zone, plural

SEND_HOUR = 8  # local time, Monday


def compose(project: Project, s: Summary, broke: list[str], settings: Settings) -> tuple[str, str]:
    number = {n.label: n for n in s.numbers}
    facts = [plural(s.visitors, "visitor")]
    if s.payments_connected:
        facts.append(plural(s.payments, "payment"))
    facts.append("nothing broke" if not broke else plural(len(broke), "thing") + " broke")
    subject = f"{project.domain} this week: {', '.join(facts)}"

    lines = ["Nothing broke this week." if not broke else "What broke this week:"]
    lines += [f"  - {sentence}" for sentence in broke]
    lines.append("")
    for label in ("Visitors", "Page views", "Payments", "Paid"):
        n = number.get(label)
        if n and n.value != "–":
            lines.append(f"{label}: {n.value}" + (f" ({n.note})" if n.note else ""))
    if s.sources:
        lines.append(f"Most visitors came from: {s.sources[0].name} ({s.sources[0].count:,}).")
    if s.sells:
        lines.append(f"Best seller: {s.sells[0].name} ({plural(s.sells[0].count, 'payment')}, {s.sells[0].amount}).")
    if s.coming_up:
        lines += ["", "Coming up:"] + [f"  - {item}" for item in s.coming_up]
    looks = [p for p in s.problems if p.level in ("look", "broken")]
    if looks:
        lines += ["", "Worth fixing when you have a minute:"] + [f"  - {p.title} {p.detail}" for p in looks]
        lines += ["", "Your coding agent can work through these. Paste this into it:",
                  "    Run npx prodbat next and fix what it reports."]
    if s.checks:
        lines += ["", f"We loaded your site from outside {plural(s.checks, 'time')} this week."]
    lines += ["", f"Your page: {settings.base}/d/{project.page_token}", "",
              "If this email ever fails to arrive on a Monday, something is wrong with us."]
    return subject, "\n".join(lines)


def local_monday_start(project: Project, now: dt.datetime) -> dt.datetime | None:
    """When this project's Monday email is due, in UTC, if that is today; else None.
    It goes from 08:00 on Monday in the owner's time zone (UTC until their page
    has told us the zone)."""
    zone = local_zone(project.timezone) or dt.UTC
    local = now.astimezone(zone)
    if local.weekday() != 0 or local.hour < SEND_HOUR:
        return None
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.UTC)


async def send_weekly(sessions, settings: Settings, now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.UTC)
    queued = 0
    async with sessions() as db:
        candidates = (await db.scalars(select(Project).where(
            Project.alert_email.is_not(None), Project.paused_at.is_(None),
            (Project.weekly_sent_at.is_(None)) | (Project.weekly_sent_at < now - dt.timedelta(days=5)))
            .with_for_update(skip_locked=True))).all()
        for project in candidates:
            monday = local_monday_start(project, now)
            if monday is None or project.created_at >= monday or \
                    (project.weekly_sent_at is not None and project.weekly_sent_at >= monday):
                continue
            project.weekly_sent_at = now
            summary = await gather(db, project, now)
            if summary.status == "waiting":
                continue  # nothing to report on a site that never sent anything
            broke = [past_tense(i) for i in await db.scalars(select(Incident).where(
                Incident.project_id == project.id, Incident.level == "broken",
                Incident.ended_at.is_not(None), Incident.started_at >= now - WEEK)
                .order_by(Incident.started_at))]
            broke += [f"{i.title}. Still going." for i in await db.scalars(select(Incident).where(
                Incident.project_id == project.id, Incident.level == "broken", Incident.ended_at.is_(None)))]
            subject, body = compose(project, summary, broke, settings)
            queue_email(db, project, settings, subject, body, sender_name="Prodbat weekly")
            queued += 1
        await db.commit()
    return queued
