"""The setup checklist the owner's coding agent works through after init.

init prints the basics (the script tag, server errors, the environment
variables, a deploy). This list carries on from there, so the agent
finishes the job: each item turns on part of what the owner gets.

An item is done when Prodbat receives its data. Some data only arrives
when the app is used (a payment, a visit), so an item the agent has
finished waits for it, and does not hold setup up. An item that does not
apply to the app (no scheduled jobs, no payments) can be skipped.

The same safety rules as tasks.py: the instructions are fixed text from
suggestions.py, filled in with this server's address and the project's
own domain and site key. Nothing a visitor, an email or a log line says
ends up in them.
"""

import datetime as dt
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .models import Project
from .suggestions import BY_ID, render

SCRIPT = "script"
# Server errors and dependencies first: every app has both. Then the
# things only some apps have. S-04 is left out: it is optional polish.
ORDER = ["S-03", "S-05", "S-07", "S-01", "S-02", "S-06"]
# Why an item cannot be skipped.
REQUIRED = {
    SCRIPT: "The script tag cannot be skipped: without it Prodbat sees no visitors and no failing requests. "
            "Add it, deploy, and run `npx prodbat verify`.",
    "S-05": "S-05 cannot be skipped. Every app is built on a language and packages, and security alerts are about "
            "them. Run `npx prodbat deps` in the project root. If there is no lockfile, the server library from S-03 "
            "reports the language version when the app starts, and that counts. A site of static files with no "
            "packages can skip S-05 once S-03 is skipped.",
}
# S-05 may be skipped once S-03 is: a site of static files may have no packages or runtime to report.
NO_SERVER_REQUIRED = {"script"}
# When an item may not apply, said next to it in the list.
MAYBE = {"S-03": "skip only if the app has no server code of its own: static files, or a front end that talks "
                 "straight to Supabase or Firebase with no functions",
         "S-01": "skip if the app takes no payments with Stripe", "S-02": "skip if the app has no scheduled jobs",
         "S-06": "skip if the app has no job queue", "S-07": "skip if the user does not want logs kept"}
# How long an item the agent has finished waits for its data before it is
# handed back: by then it should have arrived, or the change is not working.
WAIT = dt.timedelta(days=7)

SCRIPT_STEPS = """\
Prodbat has not counted a visit yet. Add this tag to the layout or template every page shares, inside <head>:

    {tag}

Deploy, then run `npx prodbat verify`. It checks the tag from outside. This item is done when the first visit is
counted, which happens when someone opens the site."""

KEEP_GOING = """\
Keep going until setup is complete. Each item turns on part of what the user gets from Prodbat.
After each change, deploy the way this project is normally deployed, then run `npx prodbat done <id>`, then
`npx prodbat setup` for the next item.
If an item truly does not apply to this app (it has no server code of its own, no scheduled jobs, takes no
payments, or has no job queue),
run `npx prodbat skip <id>` instead of inventing something to watch. If you are not sure whether it applies, ask
the user."""

AFTER = ("From now on, run `npx prodbat next` at the start of each session, and `npx prodbat verify` after each "
         "deploy.")


@dataclass
class Item:
    id: str
    title: str
    state: str  # "done", "skipped", "waiting" or "to do"
    # Set when the agent said it had finished, the wait ran out and no data came.
    came_back: dt.datetime | None = None


def _when(value) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def waiting_ids(project: Project, now: dt.datetime) -> set[str]:
    """Items the agent has finished whose data has not arrived yet, still within the wait."""
    return {sid for sid, since in (project.setup_waiting or {}).items()
            if (when := _when(since)) is not None and now - when < WAIT}


async def checklist(db: AsyncSession, project: Project, now: dt.datetime | None = None) -> list[Item]:
    now = now or dt.datetime.now(dt.UTC)
    skipped = set(project.setup_skipped or [])
    waiting = project.setup_waiting or {}

    def state_of(sid: str, done: bool) -> Item:
        title = "The script tag is on every page" if sid == SCRIPT else BY_ID[sid]["title"]
        if done:
            return Item(sid, title, "done")
        if sid in skipped:
            return Item(sid, title, "skipped")
        when = _when(waiting.get(sid))
        if when is not None:
            return Item(sid, title, "waiting") if now - when < WAIT else Item(sid, title, "to do", came_back=when)
        return Item(sid, title, "to do")

    items = [state_of(SCRIPT, project.first_data_at is not None)]
    for sid in ORDER:
        done = not await db.scalar(text(BY_ID[sid]["applies"]), {"p": project.id})
        items.append(state_of(sid, done))
    return items


def complete(items: list[Item]) -> bool:
    return all(item.state != "to do" for item in items)


def progress(items: list[Item]) -> str:
    """'Setup: 3 of 7 done, 1 waiting for its first data.'"""
    finished = sum(item.state in ("done", "skipped") for item in items)
    waiting = sum(item.state == "waiting" for item in items)
    return f"Setup: {finished} of {len(items)} done" + (f", {waiting} waiting for its first data" if waiting else "") + "."


def pointer(items: list[Item]) -> str:
    """One line for the end of another answer: what is next, or that setup is complete."""
    first = next((item for item in items if item.state == "to do"), None)
    if first is None:
        return f"{progress(items)} Setup is complete. {AFTER}"
    return f"{progress(items)} Next: {first.id}, {first.title}. Run `npx prodbat setup` for its steps."


LABEL = {"done": "done   ", "skipped": "skipped", "waiting": "waiting", "to do": "to do  "}


def render_setup(items: list[Item], project: Project, settings: Settings) -> str:
    lines = [progress(items), ""]
    for item in items:
        note = (" (finished; waiting for its first data)" if item.state == "waiting" else
                f" ({MAYBE[item.id]})" if item.state == "to do" and item.id in MAYBE else "")
        lines.append(f"  {LABEL[item.state]}  {item.id:6}  {item.title}{note}")
    lines.append("")

    first = next((item for item in items if item.state == "to do"), None)
    if first is None:
        lines.append("Setup is complete. Prodbat is watching everything that applies to this app.")
        waiting = [item.id for item in items if item.state == "waiting"]
        if waiting:
            lines.append(f"{', '.join(waiting)} {'is' if len(waiting) == 1 else 'are'} waiting for the first data, which "
                         "arrives once the change is deployed and used. There is nothing more to do for "
                         f"{'it' if len(waiting) == 1 else 'them'} now.")
        lines += ["", AFTER]
        return "\n".join(lines) + "\n"

    lines += [f"# Next: {first.id}, {first.title}", ""]
    if first.came_back:
        lines += [f"This was marked finished on {first.came_back:%Y-%m-%d}, and no data has arrived since. Check "
                  "that the change is deployed and that it runs.", ""]
    if first.id == SCRIPT:
        tag = f'<script defer src="{settings.base}/p.js" data-site="{project.key}"></script>'
        lines.append(SCRIPT_STEPS.format(tag=tag))
    else:
        lines += [render(BY_ID[first.id], project, settings.base)["instructions"], "",
                  f"When you have finished, and deployed if the change needs it, run `npx prodbat done {first.id}`."]
    lines += ["", KEEP_GOING]
    return "\n".join(lines) + "\n"


def as_dict(items: list[Item], project: Project, settings: Settings) -> dict:
    first = next((item for item in items if item.state == "to do"), None)
    return {"complete": first is None, "next": first.id if first else None,
            "items": [{"id": item.id, "title": item.title, "state": item.state} for item in items],
            "text": render_setup(items, project, settings)}


async def setup(db: AsyncSession, project: Project, settings: Settings) -> dict:
    return as_dict(await checklist(db, project), project, settings)


def _known(sid: str) -> str:
    sid = sid.strip()
    if sid.lower() == SCRIPT:
        return SCRIPT
    if sid.upper() in ORDER:
        return sid.upper()
    if sid.upper() in BY_ID:
        raise HTTPException(400, f"{sid.upper()} is not part of setup, so there is nothing to skip. "
                                 "Run `npx prodbat setup` for the list.")
    raise HTTPException(404, f"There is no setup item {sid}. Run `npx prodbat setup` for the list.")


async def skip(db: AsyncSession, project: Project, settings: Settings, sid: str) -> dict:
    sid = _known(sid)
    no_server = "S-03" in (project.setup_skipped or [])
    if sid in REQUIRED and not (no_server and sid not in NO_SERVER_REQUIRED):
        raise HTTPException(400, REQUIRED[sid])
    items = await checklist(db, project)
    if next(item for item in items if item.id == sid).state == "done":
        return {**as_dict(items, project, settings),
                "text": f"{sid} is already done: Prodbat is receiving that data, so there is nothing to skip.\n"
                        f"{pointer(items)}\n"}
    if sid not in (project.setup_skipped or []):
        project.setup_skipped = [*(project.setup_skipped or []), sid]
    project.setup_waiting = {k: v for k, v in (project.setup_waiting or {}).items() if k != sid}
    await db.commit()
    items = await checklist(db, project)
    return {**as_dict(items, project, settings),
            "text": f"Skipped {sid}, {BY_ID[sid]['title']}. If it turns out to apply after all, run "
                    f"`npx prodbat unskip {sid}`.\n{pointer(items)}\n"}


async def unskip(db: AsyncSession, project: Project, settings: Settings, sid: str) -> dict:
    sid = _known(sid)
    # S-05 could only be skipped because S-03 was, so they come back together.
    back = {sid, "S-05"} if sid == "S-03" else {sid}
    project.setup_skipped = [s for s in (project.setup_skipped or []) if s not in back]
    await db.commit()
    items = await checklist(db, project)
    return {**as_dict(items, project, settings),
            "text": f"{sid} is back on the setup list.\n{pointer(items)}\n"}


async def mark_waiting(db: AsyncSession, project: Project, sid: str, now: dt.datetime | None = None) -> None:
    """The agent says it has finished an item whose data has not arrived yet. Not committed here."""
    now = now or dt.datetime.now(dt.UTC)
    if (sid == SCRIPT or sid in ORDER) and sid not in (project.setup_skipped or []) \
            and sid not in waiting_ids(project, now):
        project.setup_waiting = {**(project.setup_waiting or {}), sid: now.isoformat()}
