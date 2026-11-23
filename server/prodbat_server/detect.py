"""Decides what counts as a problem, from what has been reported.

Most rules use direct evidence (failures that were seen). The "went quiet"
rules use silence: payments or visits stopping for longer than their usual
rate explains. Every threshold is a constant below, and all are first guesses
to tune against real sites.
"""

import datetime as dt
import math

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .flows import SAY, flow_for
from .incidents import report, resolve
from .models import Incident, Job, Project
from .traffic import VISIT_GAP
from .words import ago, every, lasted, plural

DAY = dt.timedelta(days=1)
HOUR = dt.timedelta(hours=1)
# Broken right now: one route failed for this many visitors, this recently.
BROKEN_VISITORS = 3
BROKEN_WINDOW = dt.timedelta(minutes=15)
# Paying, signing in and signing up: one failure is enough.
FLOW_BROKEN = 1
# A broken route is "working again" once nobody has hit it for this long.
QUIET_AFTER = dt.timedelta(minutes=30)
# Needs a look: the same failure reached this many visitors in a day.
LOOK_VISITORS = 2
# A scheduled job is late once it has gone this many times its usual gap.
LATE_FACTOR = 1.5
# Server errors: this many of the same one within the window is "broken".
SERVER_BROKEN_COUNT = 5
# Job queues: the oldest waiting job has waited this long.
QUEUE_STUCK_SECONDS = 15 * 60
# ...or the queue used to report every minute and has gone this quiet.
QUEUE_SILENT = dt.timedelta(minutes=15)
# ...or this many jobs failed in an hour, and they were this share of all jobs.
QUEUE_FAILED_COUNT = 5
QUEUE_FAILED_SHARE = 0.2

# Went quiet: something that normally keeps happening has stopped. The usual
# rate comes from the last 28 days up to the latest event; the alert fires when
# a silence this long would happen by chance less often than this. At two
# payments a day that is about 3.5 days of silence (a budget of roughly one
# false alarm per 50 stream-months).
QUIET_BASELINE = dt.timedelta(days=28)
QUIET_LOOK_CHANCE = 1e-3
QUIET_BROKEN_CHANCE = 1e-5
# Too little history says nothing: at least this many events over this long.
QUIET_MIN_EVENTS = {"payments": 8, "visitors": 50}
QUIET_MIN_SPAN = dt.timedelta(days=3)
# Visits, not page views (traffic.VISIT_GAP): counting page views made a site
# with three visitors a day look like one with a visit every hour.
# Quiet nights are normal on small sites: a silence must also be this many
# times longer than the longest one in the baseline.
QUIET_LONGEST_FACTOR = 1.5

MANAGED = ("route", "error", "server", "job", "queue", "quiet")


async def evaluate_project(db: AsyncSession, project: Project, settings: Settings, now: dt.datetime) -> None:
    p = {"p": project.id, "day": now - DAY, "recent": now - BROKEN_WINDOW, "quiet": now - QUIET_AFTER,
         "hour": now - HOUR}
    open_ = {(i.kind, i.key): i for i in await db.scalars(select(Incident).where(
        Incident.project_id == project.id, Incident.ended_at.is_(None), Incident.kind.in_(MANAGED)))}
    # After a problem closes, only what happened since then counts towards a new one.
    closed = {(kind, key): ended for kind, key, ended in await db.execute(
        select(Incident.kind, Incident.key, func.max(Incident.ended_at))
        .where(Incident.project_id == project.id, Incident.ended_at >= now - DAY, Incident.kind.in_(MANAGED))
        .group_by(Incident.kind, Incident.key))}
    still_wrong: set[tuple[str, str]] = set()

    async def fixed_since_claim(existing, query, params) -> bool:
        """The agent said it fixed this at least QUIET_AFTER ago, and it has not happened since."""
        claimed = existing.fix_claimed_at if existing else None
        if claimed is None or now - claimed < QUIET_AFTER:
            return False
        return not await db.scalar(text(query), {**params, "p": project.id, "claimed": claimed})

    async def say(kind, key, **fields):
        still_wrong.add((kind, key))
        fields.setdefault("data", {"name": key})
        await report(db, project, settings, kind=kind, key=key, **fields)

    # Requests from visitors' browsers that the site's own server failed.
    for r in await db.execute(text("""
        select method, route, max(status) status, count(*) n, count(distinct visitor) v,
               count(distinct visitor) filter (where ts >= :recent) recent_v,
               count(*) filter (where ts >= :quiet) quiet_n,
               min(ts) first, min(ts) filter (where ts >= :recent) recent_first
        from failed_requests where project_id = :p and ts >= :day
        group by method, route"""), p):
        key = f"{r.method} {r.route}"
        since = closed.get(("route", key))
        v = r.v if not since else await db.scalar(text("""select count(distinct visitor) from failed_requests
            where project_id = :p and method = :m and route = :r and ts > :since"""),
            {"p": project.id, "m": r.method, "r": r.route, "since": since})
        did = "did not answer" if r.status == 0 else f"answered with an error ({r.status})"
        who = f"{plural(r.v, 'visitor')} in the last 24 hours."
        flow = flow_for(project, key)
        existing = open_.get(("route", key))
        if r.recent_v >= (FLOW_BROKEN if flow else BROKEN_VISITORS):
            part = f" This is part of your {SAY[flow][1]}." if flow else ""
            await say("route", key, reach=r.v, level="broken", title=SAY[flow][0] if flow else f"{key} is failing",
                      what=f"{key} {did} for {plural(r.recent_v, 'visitor')} in the last 15 minutes.{part}",
                      who=who, started_at=r.recent_first,
                      data={"method": r.method, "route": r.route, "status": r.status, "flow": flow})
        elif existing and existing.level == "broken" and r.quiet_n > 0:
            still_wrong.add(("route", key))  # fewer people hitting it, but not quiet yet
        elif existing and existing.level == "broken":
            pass  # quiet: closed below with a "working again"
        elif await fixed_since_claim(existing, """select exists (select 1 from failed_requests where project_id = :p
                and method = :m and route = :r and ts >= :claimed)""", {"m": r.method, "r": r.route}):
            pass  # the agent's fix has held: closed below
        elif v >= LOOK_VISITORS:
            await say("route", key, reach=r.v, level="look", title=f"{key} {did}",
                      what=f"{key} {did} {plural(r.n, 'time')} in the last 24 hours.", who=who,
                      started_at=r.first,
                      data={"method": r.method, "route": r.route, "status": r.status, "flow": flow})

    # Errors in the page's own code. Errors from extensions and other sites' scripts are kept, not alerted on (noise.py).
    for r in await db.execute(text("""
        select fingerprint, max(message) message, max(file) file, max(line) line, max(path) path,
               count(distinct visitor) v, min(ts) first
        from browser_errors where project_id = :p and ts >= :day and origin = 'site'
        group by fingerprint"""), p):
        since = closed.get(("error", r.fingerprint))
        v = r.v if not since else await db.scalar(text("""select count(distinct visitor) from browser_errors
            where project_id = :p and fingerprint = :f and ts > :since and origin = 'site'"""),
            {"p": project.id, "f": r.fingerprint, "since": since})
        if v >= LOOK_VISITORS:
            where = f" ({r.file.rsplit('/', 1)[-1]}, line {r.line})" if r.file else ""
            await say("error", r.fingerprint, reach=r.v, level="look", title=f"An error in your code on {r.path}",
                      what=f"“{r.message}”{where}.", who=f"{plural(r.v, 'visitor')} in the last 24 hours.",
                      started_at=r.first, data={"path": r.path, "file": r.file, "line": r.line})

    # Errors and 5xx answers reported by the site's own server.
    for r in await db.execute(text("""
        select fingerprint, max(kind) kind, max(message) message, max(route) route, max("where") place,
               count(*) n, count(*) filter (where ts >= :recent) recent_n,
               count(*) filter (where ts >= :quiet) quiet_n,
               min(ts) first, min(ts) filter (where ts >= :recent) recent_first
        from server_errors where project_id = :p and ts >= :day
        group by fingerprint"""), p):
        since = closed.get(("server", r.fingerprint))
        n = r.n if not since else await db.scalar(text("""select count(*) from server_errors
            where project_id = :p and fingerprint = :f and ts > :since"""),
            {"p": project.id, "f": r.fingerprint, "since": since})
        on = r.route or "Your server"
        cause = f"{r.kind}: {r.message}" if r.message else r.kind
        cause += f" at {r.place}" if r.place else ""
        flow = flow_for(project, r.route) if r.route else None
        existing = open_.get(("server", r.fingerprint))
        if r.recent_n >= (FLOW_BROKEN if flow else SERVER_BROKEN_COUNT):
            part = f" This is part of your {SAY[flow][1]}." if flow else ""
            await say("server", r.fingerprint, reach=r.n, level="broken", title=SAY[flow][0] if flow else f"{on} is failing",
                      what=f"{on} failed {plural(r.recent_n, 'time')} in the last 15 minutes. Cause: {cause}.{part}",
                      who=f"{plural(r.n, 'request')} failed in the last 24 hours.", started_at=r.recent_first,
                      data={"route": r.route, "kind": r.kind, "where": r.place, "flow": flow})
        elif existing and existing.level == "broken" and r.quiet_n > 0:
            still_wrong.add(("server", r.fingerprint))
        elif existing and existing.level == "broken":
            pass
        elif await fixed_since_claim(existing, """select exists (select 1 from server_errors where project_id = :p
                and fingerprint = :f and ts >= :claimed)""", {"f": r.fingerprint}):
            pass  # the agent's fix has held: closed below
        elif n >= 1:
            await say("server", r.fingerprint, reach=r.n, level="look", title=f"{on} failed",
                      what=f"{on} failed {plural(r.n, 'time')} in the last 24 hours. Cause: {cause}.",
                      who=f"{plural(r.n, 'request')} failed.", started_at=r.first,
                      data={"route": r.route, "kind": r.kind, "where": r.place, "flow": flow})

    # Scheduled jobs that did not check in.
    for job in await db.scalars(select(Job).where(Job.project_id == project.id)):
        due = job.last_ping_at + dt.timedelta(seconds=job.every_seconds * LATE_FACTOR)
        if now > due:
            await say("job", job.name, level="broken", title=f"Your {job.name} job did not run",
                      what=f"It normally checks in {every(job.every_seconds)}. "
                           f"The last time was {ago(job.last_ping_at, now)}.",
                      who="Whatever depends on this job.",
                      todo="Check that the job is still scheduled and look at its last run, "
                           "or paste the line below into your coding agent.",
                      started_at=due)

    # Job queues: stuck, gone quiet, or failing.
    for q in await db.execute(text("""
        select name, max(ts) last, min(ts) first,
               (array_agg(waiting order by ts desc))[1] waiting,
               (array_agg(coalesce(oldest_seconds, max_wait_seconds) order by ts desc))[1] wait,
               coalesce(sum(failed) filter (where ts >= :hour), 0) failed,
               coalesce(sum(done) filter (where ts >= :hour), 0) done,
               count(*) filter (where ts >= :recent) recent_n,
               min(ts) filter (where ts >= :recent) recent_first,
               min(waiting) filter (where ts >= :recent and waiting is not null) recent_least,
               coalesce(sum(done) filter (where ts >= :recent), 0) recent_done
        from queue_reports where project_id = :p and ts >= :day
        group by name"""), p):
        todo = "Check that your workers are running and look at their logs, or paste the line below into your coding agent."
        depends = "Whatever those jobs do (emails, payments, reports) is not happening."
        waiting = f"{plural(q.waiting, 'job')} waiting" if q.waiting is not None else "Jobs are waiting"
        if q.last >= now - QUEUE_SILENT and (q.wait or 0) >= QUEUE_STUCK_SECONDS:
            await say("queue", q.name, level="broken", title=f"Jobs in your {q.name} queue are stuck",
                      what=f"{waiting}, and the oldest has waited {lasted(q.wait)}.", who=depends, todo=todo,
                      started_at=now)
        elif (q.wait is None and q.recent_n >= 3 and now - q.recent_first >= dt.timedelta(minutes=10)
              and (q.recent_least or 0) > 0 and q.recent_done == 0):
            # Nobody can say how long the oldest has waited, but nothing is getting done.
            await say("queue", q.name, level="broken", title=f"Jobs in your {q.name} queue are stuck",
                      what=f"{waiting}, and none has finished in the last 15 minutes. The workers may have stopped.",
                      who=depends, todo=todo, started_at=now)
        elif q.last < now - QUEUE_SILENT and q.last - q.first >= HOUR and (q.waiting or 0) > 0:
            await say("queue", q.name, level="broken", title=f"Your {q.name} queue has gone quiet",
                      what=f"It reported every minute and stopped {ago(q.last, now)}, with {waiting.lower()}. "
                           "The workers may have stopped.", who=depends, todo=todo, started_at=q.last)
        elif q.failed >= QUEUE_FAILED_COUNT and q.failed >= QUEUE_FAILED_SHARE * (q.failed + q.done):
            await say("queue", q.name, level="look", title=f"Jobs in your {q.name} queue are failing",
                      what=f"{plural(q.failed, 'job')} failed in the last hour, out of {q.failed + q.done:,}.",
                      who=depends, todo=todo, started_at=now)

    # Things that normally keep happening and have stopped.
    for stream, table in (("payments", "payments"), ("visitors", "pageviews")):
        found = await went_quiet(db, project, stream, table, now)
        if found:
            await say("quiet", stream, **found)

    recovery = {
        "quiet": "It has started again.",
        "route": "No visitor has hit this error for 30 minutes.",
        "server": "This error has not happened for 30 minutes.",
        "job": "The job has checked in again.",
        "queue": "The queue is moving again.",
    }
    for (kind, key), incident in open_.items():
        if (kind, key) not in still_wrong:
            resolve(db, project, incident, settings, now, recovery.get(kind))


async def went_quiet(db: AsyncSession, project: Project, stream: str, table: str, now: dt.datetime) -> dict | None:
    """If `stream` has been silent for longer than its usual rate explains,
    the fields for a problem; otherwise None. Assumes events arrive at an even
    rate (Poisson), which overstates the surprise of a quiet night, so the
    chance threshold is strict, and a silence no longer than the longest one
    in the baseline (a quiet night) never counts."""
    if stream == "visitors":  # one arrival per visit: a page view after the visitor was away VISIT_GAP
        starts = f"""select ts from (select ts, lag(ts) over (partition by visitor order by ts) prev from {table}
                      where project_id = :p and ts >= :since) v
                     where prev is null or ts - prev > :visit_gap"""
    else:
        starts = f"select ts from {table} where project_id = :p and ts >= :since"
    params = {"p": project.id, "since": now - QUIET_BASELINE, "visit_gap": VISIT_GAP}
    row = (await db.execute(text(f"""
        select count(*) n, min(ts) first, max(gap) longest,
               (select max(ts) from {table} where project_id = :p and ts >= :since) last
        from (select ts, ts - lag(ts) over (order by ts) gap from ({starts}) s) g"""), params)).one()
    if row.n < QUIET_MIN_EVENTS[stream] or row.last - row.first < QUIET_MIN_SPAN:
        return None
    gap = (row.last - row.first).total_seconds() / (row.n - 1)  # the usual time between arrivals
    silence = (now - row.last).total_seconds()
    longest = row.longest.total_seconds() if row.longest else 0
    chance = math.exp(-silence / gap)
    if chance >= QUIET_LOOK_CHANCE or silence <= QUIET_LONGEST_FACTOR * longest:
        return None
    level = "broken" if chance < QUIET_BROKEN_CHANCE else "look"
    usual = lasted(gap)
    before = f" The longest quiet stretch in the last 4 weeks was {lasted(longest)}." if longest >= HOUR.total_seconds() else ""
    if stream == "payments":
        return {"level": level, "title": "Payments have stopped",
                "what": f"The last payment came in {ago(row.last, now)}. You normally get one about every {usual}.{before}",
                "who": "Anyone trying to pay.",
                "todo": "Try buying something yourself, and look in Stripe for failed payments or webhook deliveries; "
                        "or paste the line below into your coding agent.",
                "started_at": row.last, "data": {"stream": stream, "usual_seconds": int(gap)}}
    return {"level": level, "title": "Visitors have stopped being counted",
            "what": f"The last visit was counted {ago(row.last, now)}. You normally get one about every {usual}.{before} "
                    "Either the site is not reaching people, or the Prodbat script was removed in a deploy.",
            "who": "Possibly every visitor.",
            "todo": "Open your site and check it loads, and that the Prodbat script tag is still in the page; "
                    "or paste the line below into your coding agent.",
            "started_at": row.last, "data": {"stream": stream, "usual_seconds": int(gap)}}


async def evaluate_all(sessions, settings: Settings) -> None:
    now = dt.datetime.now(dt.UTC)
    async with sessions() as db:
        for project in (await db.scalars(select(Project).where(Project.paused_at.is_(None)))).all():
            await evaluate_project(db, project, settings, now)
            await db.commit()
