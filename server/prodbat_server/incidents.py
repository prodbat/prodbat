"""Opening and closing problems, and the emails that go with them.

Every alert answers four questions in the same order: what broke, since
when, who is affected, what to do. There are two subject prefixes,
"Broken:" and "Needs a look:", and "Working again:" closes a broken one
in the same thread.
"""

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .models import Email, Incident, Mute, Project
from . import github, push, slack, telegram, webhooks
from .texts import queue_text
from .words import clock, lasted

PREFIX = {"broken": "Broken", "look": "Needs a look"}


def agent_prompt(project: Project, incident: Incident) -> str:
    """What the owner pastes into their coding agent: it names this problem's own task."""
    from .tasks import from_incident
    task = from_incident(incident) if incident.id is not None else None
    if task is None:
        return (f"{project.domain} has a problem: {incident.title}. Run `npx prodbat next` in this project for the "
                f"steps and the evidence, check them against the code before changing anything, fix it and ship "
                f"the fix the way this project usually ships, then run `npx prodbat done` with the task id it printed.")
    return (f"{project.domain} has a problem: {incident.title}. Run `npx prodbat task {task.id}` in this project for "
            f"the steps and the evidence, check them against the code before changing anything, fix it and ship the "
            f"fix the way this project usually ships, then run `npx prodbat done {task.id}`.")


def new_message_id(settings: Settings) -> str:
    host = settings.mail_from.rsplit("@", 1)[-1].strip("> ") or "localhost"
    return f"<{uuid.uuid4().hex}@{host}>"


def queue_email(db: AsyncSession, project: Project, settings: Settings, subject: str, body: str,
                in_reply_to: str | None = None, sender_name: str = "Prodbat") -> str | None:
    if not project.alert_email or project.paused_at is not None:
        return None
    message_id = new_message_id(settings)
    db.add(Email(project_id=project.id, to=project.alert_email, subject=subject, body=body,
                 message_id=message_id, in_reply_to=in_reply_to, sender_name=sender_name))
    return message_id


DEFAULT_TODO = "Paste the line below into your coding agent, or roll back your last deploy."


def alert_body(project: Project, incident: Incident, settings: Settings) -> str:
    todo = incident.todo or DEFAULT_TODO
    return "\n".join([
        f"What broke: {incident.what}",
        f"Since when: {clock(incident.started_at, project.timezone)}.",
        f"Who is affected: {incident.who}",
        f"What to do: {todo}",
        "",
        "For your coding agent:",
        "",
        f"    {agent_prompt(project, incident)}",
        "",
        f"Your page: {settings.base}/d/{project.page_token}",
        "",
        "If this is not a problem, open your page and press \"This is fine, stop telling me\".",
    ])


def queue_alert(db: AsyncSession, project: Project, incident: Incident, settings: Settings,
                updated: bool = False) -> None:
    subject = f"{PREFIX[incident.level]}: {incident.title} on {project.domain}"
    message_id = queue_email(db, project, settings, subject, alert_body(project, incident, settings),
                             in_reply_to=incident.message_id)
    incident.message_id = incident.message_id or message_id
    slack.post_alert(db, project, incident, settings, PREFIX[incident.level], incident.todo or DEFAULT_TODO,
                     agent_prompt(project, incident), clock(incident.started_at, project.timezone))
    telegram.post_alert(db, project, incident, settings, PREFIX[incident.level], incident.todo or DEFAULT_TODO,
                        agent_prompt(project, incident), clock(incident.started_at, project.timezone))
    push.post_alert(db, project, incident, settings, PREFIX[incident.level])
    webhooks.post_alert(db, project, incident, settings, updated)
    github.post_alert(db, project, incident, settings)
    if incident.level == "broken":  # texts only when customers are affected now
        queue_text(db, project, settings, f"Prodbat. {subject}. Since {clock(incident.started_at, project.timezone)}. "
                                          f"{settings.base}/d/{project.page_token}")


# A problem the coding agent dismissed comes back once it reaches at least
# this many visitors (or server errors) and twice what it had then.
DISMISS_REGROW = 10


async def is_muted(db: AsyncSession, project: Project, kind: str, key: str) -> bool:
    return await db.scalar(select(Mute.id).where(Mute.project_id == project.id, Mute.kind == kind,
                                                 Mute.key == key)) is not None


async def report(db: AsyncSession, project: Project, settings: Settings, *, kind: str, key: str = "",
                 level: str, title: str, what: str, who: str, todo: str = "",
                 started_at: dt.datetime, gather: list | None = None, data: dict | None = None,
                 reach: int | None = None) -> Incident | None:
    """Say that something is wrong. Opens a problem and emails once; while
    it stays open, later calls only refresh the wording, except that a
    'look' that becomes 'broken' is emailed again.

    With `gather`, a new problem is added to that list instead of being
    emailed, so the caller can send several as one email (see `digest`)."""
    mute = await db.scalar(select(Mute).where(Mute.project_id == project.id, Mute.kind == kind, Mute.key == key))
    if mute is not None:
        # The owner's "this is fine" is for good. The agent's dismissal holds while the
        # problem stays small, and ends if it grows or starts breaking the site.
        grown = reach is not None and reach >= max(2 * (mute.reach or 0), DISMISS_REGROW)
        if mute.by != "agent" or not (grown or level == "broken"):
            return None
        data = {**(data or {}), "dismissed": mute.reason, "dismissed_reach": mute.reach}
        await db.delete(mute)
    incident = await db.scalar(select(Incident).where(
        Incident.project_id == project.id, Incident.kind == kind, Incident.key == key,
        Incident.ended_at.is_(None)))
    if incident is None:
        incident = Incident(project_id=project.id, kind=kind, key=key, level=level, title=title, what=what,
                            who=who, todo=todo, started_at=started_at, data=data or {})
        db.add(incident)
        await db.flush()  # the id names its task in the prompt for the coding agent
        if gather is None:
            queue_alert(db, project, incident, settings)
        else:
            gather.append(incident)
        return incident
    worse = incident.level == "look" and level == "broken"
    if worse or incident.level == level:
        incident.title, incident.what, incident.who, incident.todo = title, what, who, todo
        incident.data = data or incident.data
    if worse:
        incident.level = "broken"
        queue_alert(db, project, incident, settings, updated=True)
    return incident


def digest(db: AsyncSession, project: Project, new: list[Incident], settings: Settings, about: str) -> None:
    """One email for several new problems of the same sort, so a first
    look at a project's versions is not a dozen separate alerts."""
    if len(new) == 1:
        queue_alert(db, project, new[0], settings)
    elif new:
        lines = [f"{len(new)} things about {about} need a look. None of them is breaking your site right now.", ""]
        for incident in new:
            lines += [f"- {incident.title}", f"  {incident.what}", f"  What to do: {incident.todo}", ""]
        lines += [f"Your page: {settings.base}/d/{project.page_token}", "",
                  "If one of these is not a problem, open your page and press \"This is fine, stop telling me\"."]
        queue_email(db, project, settings,
                    f"Needs a look: {len(new)} things about {about} on {project.domain}", "\n".join(lines))
        slack.post_digest(db, project, settings, new, about)
        telegram.post_digest(db, project, settings, new, about)
        push.post_digest(db, project, settings, new, about)
        for incident in new:  # one each, for agents and issues
            webhooks.post_alert(db, project, incident, settings)
            github.post_alert(db, project, incident, settings)


def resolve(db: AsyncSession, project: Project, incident: Incident, settings: Settings, now: dt.datetime,
            recovery: str | None = None) -> None:
    """Close a problem. A broken one gets a "Working again" email when
    there is a `recovery` sentence to send."""
    incident.ended_at = now
    if incident.level == "broken" and recovery:
        duration = lasted((now - incident.started_at).total_seconds())
        body = f"{recovery}\n\nIt was broken for {duration}, from {clock(incident.started_at, project.timezone)}.\n\n" \
               f"Your page: {settings.base}/d/{project.page_token}"
        queue_email(db, project, settings, f"Working again: {incident.title} on {project.domain}", body,
                    in_reply_to=incident.message_id)
        queue_text(db, project, settings, f"Prodbat. Working again: {incident.title} on {project.domain}, "
                                          f"after {duration}.")
        slack.post_recovery(db, project, incident, settings, recovery, duration)
        telegram.post_recovery(db, project, incident, settings, recovery, duration)
        push.post_recovery(db, project, incident, settings, recovery, duration)
    webhooks.post_resolved(db, project, incident, settings, recovery)
    github.post_recovery(db, project, incident, settings, recovery, clock(now, project.timezone))
