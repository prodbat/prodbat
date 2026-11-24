"""The setup checklist the owner's coding agent works through after init.

init prints the basics (the script tag, server errors, the environment
variables, a deploy). This list carries on from there, so the agent
finishes the job: each item turns on part of what the owner gets.

An item is done when Prodbat receives its data. Some data only arrives
when the app is used (a payment, a visit), so an item the agent has
finished waits for it, and does not hold setup up. An item that does not
apply to the app (no scheduled jobs, no payments) can be skipped, with a
reason the checklist shows (init skips the ones it finds no sign of).

The agent makes every change first and ships them together the way the
project usually ships (a pull request or a deploy), so setup finishes in one
pass. `npx prodbat done` takes every id it finished at once.
When nothing is left, the checklist ends with a message for the agent to
pass on to the user.

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
# A skip reason is one short line.
REASON_MAX = 120
# Items `npx prodbat done` takes at once.
MOST_AT_ONCE = 20

SCRIPT_STEPS = """\
Prodbat has not counted a visit yet. Add this tag to the layout or template every page shares, inside <head>:

    {tag}

Once the changes are live, `npx prodbat verify` checks the tag from outside. This item is done when the
first visit is counted, which happens when someone opens the site."""

KEEP_GOING = """\
Keep going until setup is complete. Each item turns on part of what the user gets from Prodbat.
Do not ship between items. Make the change for every item that is left first{all_steps}, then ship them
together the way this project usually ships (a pull request, a deploy, whatever you normally do). Then run
{done}then `npx prodbat setup` to see what is left.
If an item truly does not apply to this app (it has no server code of its own, no scheduled jobs, takes no
payments, or has no job queue),
run `npx prodbat skip <id>` instead of inventing something to watch. If you are not sure whether it applies, ask
the user."""
# Said in KEEP_GOING when only the next item's steps are shown.
ALL_STEPS = " (`npx prodbat setup --all` prints the steps for all of them)"

AFTER = ("From now on, run `npx prodbat next` at the start of each session, and `npx prodbat verify` after each "
         "deploy.")

# What each item turns on, said to the user, and when its first data comes.
TURNS_ON = {SCRIPT: "visitors, errors in the page and requests that fail", "S-03": "errors from the app's server",
            "S-05": "security and end-of-life alerts for the packages the app is built on", "S-07": "the app's logs",
            "S-01": "payments by product", "S-02": "an alert when a scheduled job stops running",
            "S-06": "an alert when the job queue gets stuck or keeps failing"}
FIRST_DATA = {SCRIPT: "when someone opens the site", "S-03": "when the new version of the app starts",
              "S-05": "when the new version of the app starts", "S-07": "when the app next writes a log line",
              "S-01": "with the next payment", "S-02": "the next time the job runs",
              "S-06": "once the queue does some work"}


@dataclass
class Item:
    id: str
    title: str
    state: str  # "done", "skipped", "waiting" or "to do"
    # Set when the agent said it had finished, the wait ran out and no data came.
    came_back: dt.datetime | None = None
    reason: str | None = None  # why it was skipped, when a reason was given


def _when(value) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _entry_id(entry) -> str | None:
    return entry if isinstance(entry, str) else entry.get("id") if isinstance(entry, dict) else None


def skipped(project: Project) -> dict[str, str | None]:
    """Skipped items and their reasons. setup_skipped is a list: an entry is an id, as it always was,
    or {"id": ..., "reason": ...} when a reason was given."""
    out = {}
    for entry in project.setup_skipped or []:
        if isinstance(sid := _entry_id(entry), str):
            reason = entry.get("reason") if isinstance(entry, dict) else None
            out[sid] = reason if isinstance(reason, str) else None
    return out


def clean_reason(reason: str | None) -> str | None:
    """One short line. It comes from the project's own key holder (init or its agent) and is shown back as a
    note in the checklist, so it is kept plain: no line breaks, no backticks."""
    reason = " ".join(str(reason or "").replace("`", "'").split())[:REASON_MAX].rstrip(" .")
    return reason or None


def waiting_ids(project: Project, now: dt.datetime) -> set[str]:
    """Items the agent has finished whose data has not arrived yet, still within the wait."""
    return {sid for sid, since in (project.setup_waiting or {}).items()
            if (when := _when(since)) is not None and now - when < WAIT}


async def checklist(db: AsyncSession, project: Project, now: dt.datetime | None = None) -> list[Item]:
    now = now or dt.datetime.now(dt.UTC)
    skips = skipped(project)
    waiting = project.setup_waiting or {}

    def state_of(sid: str, done: bool) -> Item:
        title = "The script tag is on every page" if sid == SCRIPT else BY_ID[sid]["title"]
        if done:
            return Item(sid, title, "done")
        if sid in skips:
            return Item(sid, title, "skipped", reason=skips[sid])
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
        return (f"{progress(items)} Setup is complete. Run `npx prodbat setup` for the message to pass on to the "
                f"user. {AFTER}")
    return f"{progress(items)} Next: {first.id}, {first.title}. Run `npx prodbat setup` for its steps."


LABEL = {"done": "done   ", "skipped": "skipped", "waiting": "waiting", "to do": "to do  "}


def _listing(items: list[Item]) -> list[str]:
    lines = []
    for item in items:
        note = (" (finished; waiting for its first data)" if item.state == "waiting" else
                f" ({item.reason})" if item.state == "skipped" and item.reason else
                f" ({MAYBE[item.id]})" if item.state == "to do" and item.id in MAYBE else "")
        lines.append(f"  {LABEL[item.state]}  {item.id:6}  {item.title}{note}")
    return lines


def _steps(item: Item, project: Project, settings: Settings) -> list[str]:
    lines = []
    if item.came_back:
        lines += [f"This was marked finished on {item.came_back:%Y-%m-%d}, and no data has arrived since. Check "
                  "that the change is deployed and that it runs.", ""]
    if item.id == SCRIPT:
        tag = f'<script defer src="{settings.base}/p.js" data-site="{project.key}"></script>'
        lines.append(SCRIPT_STEPS.format(tag=tag))
    else:
        lines += [render(BY_ID[item.id], project, settings.base, in_setup=True)["instructions"], "",
                  f"When you have made this change, go on to the next item. Do not ship it on its own: once "
                  f"everything ships together, `npx prodbat done` counts {item.id} with the others."]
    return lines


def _keep_going(left: list[Item], showing_all: bool) -> str:
    ids = [item.id for item in left]
    done = f"`npx prodbat done {' '.join(ids)}` (the ones you finished), " if ids else ""
    return KEEP_GOING.format(all_steps="" if showing_all or len(left) < 2 else ALL_STEPS, done=done)


def handoff(items: list[Item], project: Project, activation: dict | None = None) -> list[str]:
    """What the agent tells the user once setup is complete, in plain words. Every value in it is checked:
    the items' states, the project's own domain, and the activation link this server made."""
    state = {item.id: item.state for item in items}
    on = [TURNS_ON[item.id] for item in items if item.state == "done"]
    waiting = [item for item in items if item.state == "waiting"]
    lines = ["Tell the user:", "", f"  Prodbat is now watching {project.domain}." + (" Turned on:" if on else ""),
             *(f"  - {what}" for what in on)]
    if waiting:
        lines += ["", "  Ready, and waiting for the first data, which is normal (nothing needs doing for these):",
                  *(f"  - {TURNS_ON[item.id]}: starts {FIRST_DATA[item.id]}" for item in waiting)]
    yours = []
    if activation and activation.get("claim_url"):
        inbox = (f"click Activate in the email from Prodbat (sent to {activation['activation_sent_to']}), or "
                 if activation.get("activation_sent_to") else "")
        yours.append(f"Activate your dashboard: {inbox}open {activation['claim_url']} and sign in. Until then no "
                     "alerts are sent.")
    if state.get("S-03") not in ("done", "skipped"):
        # The server library reports in as soon as PRODBAT_KEY and PRODBAT_URL are set where the app runs.
        yours.append("If they are not there yet, add PRODBAT_KEY and PRODBAT_URL (they are in the project's .env "
                     "file) to the environment variables where the app is hosted.")
    if state.get(SCRIPT) != "done":
        yours.append("If the changes are not live yet, ship them: merge the pull request, or deploy. Prodbat starts "
                     "counting visits once they are.")
    if yours:
        lines += ["", "  What only you can do:", *(f"  - {line}" for line in yours)]
    else:
        lines += ["", "  There is nothing you need to do. Prodbat emails you when something breaks."]
    return lines


def render_setup(items: list[Item], project: Project, settings: Settings, show_all: bool = False,
                 activation: dict | None = None) -> str:
    lines = [progress(items), "", *_listing(items), ""]
    left = [item for item in items if item.state == "to do"]
    if not left:
        lines.append("Setup is complete. Prodbat is watching everything that applies to this app.")
        lines += ["", *handoff(items, project, activation), "", AFTER]
        return "\n".join(lines) + "\n"

    for item in left if show_all else left[:1]:
        lines += [f"# {'Next' if item is left[0] else 'Then'}: {item.id}, {item.title}", "",
                  *_steps(item, project, settings), ""]
    lines.append(_keep_going(left, show_all))
    return "\n".join(lines) + "\n"


def as_dict(items: list[Item], project: Project, settings: Settings, show_all: bool = False,
            activation: dict | None = None) -> dict:
    first = next((item for item in items if item.state == "to do"), None)
    return {"complete": first is None, "next": first.id if first else None,
            "items": [{"id": item.id, "title": item.title, "state": item.state,
                       **({"reason": item.reason} if item.reason else {})} for item in items],
            "text": render_setup(items, project, settings, show_all, activation)}


async def setup(db: AsyncSession, project: Project, settings: Settings, show_all: bool = False,
                activation: dict | None = None) -> dict:
    return as_dict(await checklist(db, project), project, settings, show_all, activation)


def _known(sid: str, doing: str = "skip") -> str:
    sid = sid.strip()
    if sid.lower() == SCRIPT:
        return SCRIPT
    if sid.upper() in ORDER:
        return sid.upper()
    if sid.upper() in BY_ID:
        raise HTTPException(400, f"{sid.upper()} is not part of setup, so there is nothing to {doing}. "
                                 "Run `npx prodbat setup` for the list.")
    raise HTTPException(404, f"There is no setup item {sid}. Run `npx prodbat setup` for the list.")


def _suggestion_only(sid: str) -> str | None:
    """A suggestion outside setup (S-04, serving the script from the app's own domain), or None."""
    sid = sid.strip().upper()
    return sid if sid in BY_ID and sid not in ORDER else None


async def decline(db: AsyncSession, project: Project, sid: str, reason: str | None) -> dict:
    """The user does not want a suggestion that is not part of setup: it is not offered again,
    in status, in next or as a task. Kept with the setup skips, which suggestions already leave out."""
    reason = clean_reason(reason)
    rest = [e for e in project.setup_skipped or [] if _entry_id(e) != sid]
    project.setup_skipped = [*rest, {"id": sid, "reason": reason} if reason else sid]
    await db.commit()
    return {"id": sid, "skipped": True, "reason": reason,
            "text": f"Declined {sid}, {BY_ID[sid]['title']}{f' ({reason})' if reason else ''}. Prodbat will not "
                    f"suggest it again. If that changes, run `npx prodbat unskip {sid}`.\n"}


async def skip(db: AsyncSession, project: Project, settings: Settings, sid: str, reason: str | None = None) -> dict:
    if suggestion := _suggestion_only(sid):
        return await decline(db, project, suggestion, reason)
    sid = _known(sid)
    skips = skipped(project)
    if sid in REQUIRED and not ("S-03" in skips and sid not in NO_SERVER_REQUIRED):
        raise HTTPException(400, REQUIRED[sid])
    items = await checklist(db, project)
    if next(item for item in items if item.id == sid).state == "done":
        return {**as_dict(items, project, settings),
                "text": f"{sid} is already done: Prodbat is receiving that data, so there is nothing to skip.\n"
                        f"{pointer(items)}\n"}
    reason = clean_reason(reason)
    if sid not in skips or (reason and skips[sid] != reason):
        rest = [e for e in project.setup_skipped or [] if _entry_id(e) != sid]
        project.setup_skipped = [*rest, {"id": sid, "reason": reason} if reason else sid]
    project.setup_waiting = {k: v for k, v in (project.setup_waiting or {}).items() if k != sid}
    await db.commit()
    items = await checklist(db, project)
    return {**as_dict(items, project, settings),
            "text": f"Skipped {sid}, {BY_ID[sid]['title']}{f' ({reason})' if reason else ''}. If it turns out to "
                    f"apply after all, run `npx prodbat unskip {sid}`.\n{pointer(items)}\n"}


async def unskip(db: AsyncSession, project: Project, settings: Settings, sid: str) -> dict:
    if suggestion := _suggestion_only(sid):
        project.setup_skipped = [e for e in project.setup_skipped or [] if _entry_id(e) != suggestion]
        await db.commit()
        return {"id": suggestion, "skipped": False,
                "text": f"{suggestion} is back: Prodbat will suggest it again while it applies.\n"}
    sid = _known(sid)
    # S-05 could only be skipped because S-03 was, so they come back together.
    back = {sid, "S-05"} if sid == "S-03" else {sid}
    project.setup_skipped = [e for e in project.setup_skipped or [] if _entry_id(e) not in back]
    await db.commit()
    items = await checklist(db, project)
    return {**as_dict(items, project, settings),
            "text": f"{sid} is back on the setup list.\n{pointer(items)}\n"}


async def mark_waiting(db: AsyncSession, project: Project, sid: str, now: dt.datetime | None = None) -> None:
    """The agent says it has finished an item whose data has not arrived yet. Not committed here."""
    now = now or dt.datetime.now(dt.UTC)
    if (sid == SCRIPT or sid in ORDER) and sid not in skipped(project) \
            and sid not in waiting_ids(project, now):
        project.setup_waiting = {**(project.setup_waiting or {}), sid: now.isoformat()}


async def finish(db: AsyncSession, project: Project, settings: Settings, ids: list[str], activation_of=None) -> dict:
    """The agent made the change for these items and shipped them together: each one is done, or finished and
    waiting for its first data. The whole checklist follows, so the same answer says what is left."""
    if not ids:
        raise HTTPException(400, "Which setup items did you finish? For example: npx prodbat done S-03 S-05 S-07")
    if len(ids) > MOST_AT_ONCE:
        raise HTTPException(400, f"Name at most {MOST_AT_ONCE} setup items at once.")
    wanted = []
    for raw in ids:
        raw = str(raw).strip()
        if raw[:1] in ("T", "t") and raw[1:].isdigit():
            raise HTTPException(400, f"{raw} is a task, not a setup item. Run `npx prodbat done {raw}` on its own.")
        if (sid := _known(raw, "mark")) not in wanted:
            wanted.append(sid)
    now = dt.datetime.now(dt.UTC)
    before = {item.id: item.state for item in await checklist(db, project, now)}
    for sid in wanted:
        # The script tag too: when the project ships through a pull request, nothing is live yet.
        # It counts as done only once a visit arrives.
        if before[sid] == "to do":
            await mark_waiting(db, project, sid, now)
    await db.commit()
    items = await checklist(db, project, now)
    state = {item.id: item.state for item in items}
    said = []
    for sid in wanted:
        if state[sid] == "done":
            said.append(f"{sid}: done. Prodbat is receiving it.")
        elif state[sid] == "skipped":
            said.append(f"{sid}: skipped, so there is nothing to count. If it does apply, run "
                        f"`npx prodbat unskip {sid}` and then this again.")
        elif state[sid] == "waiting":
            said.append(f"{sid}: finished, waiting for its first data, which comes {FIRST_DATA[sid]}.")
        else:
            said.append(f"{sid}: still to do.")
    # Whether the owner still has to activate the dashboard matters only for the hand-off message at the end.
    activation = await activation_of() if activation_of and complete(items) else None
    return {**as_dict(items, project, settings, activation=activation),
            "text": "\n".join(said) + "\n\n" + render_setup(items, project, settings, activation=activation)}
