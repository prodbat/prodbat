"""Everything the page, the weekly email and `prodbat status` say about a
site, gathered once."""

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .freshness import PRODUCTS, majors_behind, patch_behind
from .models import Dependency, Incident, Job, LogLine, Mute, Project, SiteCheck
from .suggestions import first_suggestion
from .words import ago, compared, day, lasted, money, plural

WEEK = dt.timedelta(days=7)
DAY = dt.timedelta(days=1)
SENTENCE = {
    "waiting": "Waiting for your first visitor.",
    "working": "Everything is working.",
    "look": "Something needs a look.",
    "broken": "Your site is broken right now.",
}


@dataclass
class Number:
    label: str
    value: str
    note: str = ""


@dataclass
class Row:
    name: str
    count: int
    tag: str = ""
    amount: str = ""


@dataclass
class Problem:
    level: str  # broken, look or quiet
    title: str
    detail: str
    id: int | None = None  # set when it is an open problem that can be muted
    kind: str = ""
    key: str = ""
    since: dt.datetime | None = None
    todo: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass
class Summary:
    domain: str
    status: str
    proof: str
    numbers: list[Number] = field(default_factory=list)
    pages: list[Row] = field(default_factory=list)
    sources: list[Row] = field(default_factory=list)
    sells: list[Row] = field(default_factory=list)
    payments_connected: bool = False
    last_payment: str = ""
    problems: list[Problem] = field(default_factory=list)
    # (date, sentence) for broken things that are over, newest first.
    happened: list[tuple[str, str]] = field(default_factory=list)
    coming_up: list[str] = field(default_factory=list)
    suggestion: dict | None = None
    # Scheduled jobs and job queues: (name, what it is doing).
    work: list[tuple[str, str]] = field(default_factory=list)
    # The detail behind the version lines in "coming up", for `status`.
    package_notes: list[str] = field(default_factory=list)
    has_logs: bool = False
    visitors: int = 0
    payments: int = 0
    checks: int = 0
    # Where this week's buyers came from, when payments carry the script's reference.
    buyers: list[Row] = field(default_factory=list)

    @property
    def sentence(self) -> str:
        return SENTENCE[self.status]


def past_tense(incident: Incident) -> str:
    duration = lasted((incident.ended_at - incident.started_at).total_seconds())
    if incident.kind == "down":
        return f"Your site did not load for {duration}."
    if incident.kind == "job":
        return f"Your {incident.key} job did not run for {duration}."
    if incident.kind in ("page", "queue"):
        return f"{incident.title.replace(' is not loading', ' did not load').replace(' are stuck', ' were stuck').replace(' has gone quiet', ' went quiet')} for {duration}."
    if incident.title.startswith("People cannot "):
        return f"{incident.title.replace('cannot', 'could not')} for {duration}."
    return f"{incident.title.replace(' is failing', ' was failing')} for {duration}."


async def evidence_for(db: AsyncSession, project: Project, incident: Incident) -> list[str]:
    """The raw facts a coding agent needs, as lines of text, followed by
    whatever the app logged at warning or error level around that time."""
    from .api import format_line

    facts = await facts_for(db, project, incident)
    nearby = (await db.scalars(select(LogLine).where(
        LogLine.project_id == project.id, LogLine.level.in_(("warning", "error")),
        LogLine.ts >= incident.started_at - dt.timedelta(minutes=5),
        LogLine.ts <= incident.started_at + dt.timedelta(minutes=10)).order_by(LogLine.ts).limit(15))).all()
    if nearby:
        facts += ["Logged around that time:", *[format_line(line) for line in nearby]]
    return facts


async def facts_for(db: AsyncSession, project: Project, incident: Incident) -> list[str]:
    p = {"p": project.id, "since": incident.started_at - DAY}
    if incident.kind == "route":
        method, _, route = incident.key.partition(" ")
        rows = await db.execute(text("""
            select ts, path, status from failed_requests
            where project_id = :p and method = :m and route = :r and ts >= :since
            order by ts desc limit 8"""), {**p, "m": method, "r": route})
        return [f"{r.ts:%Y-%m-%d %H:%M:%S}Z  {incident.key} -> {r.status or 'no answer'}  (visitor was on {r.path})"
                for r in rows]
    if incident.kind == "error":
        r = (await db.execute(text("""
            select message, file, line, col, stack, path, ts from browser_errors
            where project_id = :p and fingerprint = :f order by ts desc limit 1"""),
            {**p, "f": incident.key})).first()
        if r is None:
            return []
        return [f"{r.message}", f"at {r.file}:{r.line}:{r.col}, on page {r.path}", *r.stack.splitlines()[:15]]
    if incident.kind == "server":
        r = (await db.execute(text("""
            select kind, message, route, "where" place, trace from server_errors
            where project_id = :p and fingerprint = :f order by ts desc limit 1"""),
            {**p, "f": incident.key})).first()
        if r is None:
            return []
        return [f"{r.kind}: {r.message}" if r.message else r.kind, f"route: {r.route or 'unknown'}",
                *([f"at {r.place}"] if r.place else []), *r.trace.splitlines()[-25:]]
    if incident.kind in ("down", "page"):
        rows = await db.execute(text("""
            select ts, path, status, error from site_checks
            where project_id = :p and ok is false and path = :path order by ts desc limit 5"""),
            {**p, "path": incident.key or "/"})
        return [f"{r.ts:%Y-%m-%d %H:%M:%S}Z  GET https://{project.domain}{r.path} -> {r.status or r.error}" for r in rows]
    if incident.kind == "queue":
        rows = await db.execute(text("""
            select ts, system, waiting, oldest_seconds, max_wait_seconds, done, failed from queue_reports
            where project_id = :p and name = :n order by ts desc limit 5"""), {**p, "n": incident.key})
        return [f"{r.ts:%Y-%m-%d %H:%M:%S}Z  {r.system} queue {incident.key}: waiting={r.waiting} "
                f"oldest_seconds={r.oldest_seconds} max_wait_seconds={r.max_wait_seconds} done={r.done} failed={r.failed}"
                for r in rows]
    return []


async def gather(db: AsyncSession, project: Project, now: dt.datetime, with_evidence: bool = False) -> Summary:
    p = {"p": project.id, "now": now, "week": now - WEEK, "fortnight": now - 2 * WEEK, "day": now - DAY}

    last_heard = await db.scalar(text("""
        select max(ts) from (
            select max(ts) ts from pageviews where project_id = :p
            union all select max(ts) from browser_errors where project_id = :p
            union all select max(ts) from failed_requests where project_id = :p
            union all select max(ts) from server_errors where project_id = :p
        ) t"""), p)
    last_check = await db.scalar(
        select(SiteCheck).where(SiteCheck.project_id == project.id).order_by(SiteCheck.ts.desc()).limit(1))
    open_ = (await db.scalars(select(Incident).where(
        Incident.project_id == project.id, Incident.ended_at.is_(None)).order_by(Incident.started_at))).all()

    problems = []
    for incident in open_:
        problems.append(Problem(
            incident.level, incident.title + ".",
            f"{incident.what} Affected: {incident.who} Since {ago(incident.started_at, now)}.",
            incident.id, incident.kind, incident.key, incident.started_at, incident.todo,
            await evidence_for(db, project, incident) if with_evidence else []))

    if last_heard is None and not problems:
        loads = " Your site loads from outside." if last_check and last_check.ok else ""
        return Summary(project.domain, "waiting",
                       "Nothing has arrived from your site yet. Once the script is on your pages, "
                       "open your site and come back." + loads)

    # Things that were seen but are not (yet) a problem: one visitor, one time.
    covered = {(i.kind, i.key) for i in open_}
    covered |= {(m.kind, m.key) for m in await db.scalars(select(Mute).where(Mute.project_id == project.id))}
    for r in await db.execute(text("""
        select method, route, max(status) status, count(distinct visitor) v, max(ts) last
        from failed_requests where project_id = :p and ts >= :day and ts < :now
        group by method, route order by last desc limit 5"""), p):
        if ("route", f"{r.method} {r.route}") not in covered:
            did = "did not answer" if r.status == 0 else f"answered with an error ({r.status})"
            problems.append(Problem("quiet", f"{r.method} {r.route} {did}.",
                                    f"{plural(r.v, 'visitor')} affected, most recently {ago(r.last, now)}. "
                                    "Not flagged."))
    for r in await db.execute(text("""
        select fingerprint, max(message) message, max(path) path, count(distinct visitor) v, max(ts) last
        from browser_errors where project_id = :p and ts >= :day and ts < :now
        group by fingerprint order by last desc limit 5"""), p):
        if ("error", r.fingerprint) not in covered:
            problems.append(Problem("quiet", f"An error in your code on {r.path}.",
                                    f"“{r.message}”. {plural(r.v, 'visitor')} affected, most recently "
                                    f"{ago(r.last, now)}. Not flagged."))
    order = {"broken": 0, "look": 1, "quiet": 2}
    problems.sort(key=lambda problem: order[problem.level])
    levels = {problem.level for problem in problems}
    status = "broken" if "broken" in levels else "look" if "look" in levels else "working"

    # Visitor hashes change every day, so a distinct count over a week is
    # the sum of each day's visitors, the same way Plausible counts.
    visitors, views, visitors_before, views_before = (await db.execute(text("""
        select count(distinct visitor) filter (where ts >= :week),
               count(*) filter (where ts >= :week),
               count(distinct visitor) filter (where ts < :week),
               count(*) filter (where ts < :week)
        from pageviews where project_id = :p and ts >= :fortnight and ts < :now"""), p)).one()
    hit = await db.scalar(text("""
        select count(distinct visitor) from (
            select visitor from browser_errors where project_id = :p and ts >= :week and ts < :now
            union all
            select visitor from failed_requests where project_id = :p and ts >= :week and ts < :now
        ) t"""), p)
    has_last_week = project.created_at <= now - WEEK

    # Money. A site that sells in several currencies gets its biggest one here.
    by_currency = (await db.execute(text("""
        select currency, count(*) filter (where ts >= :week) n,
               coalesce(sum(amount) filter (where ts >= :week), 0) total,
               count(*) filter (where ts < :week) n_before,
               coalesce(sum(amount) filter (where ts < :week), 0) total_before
        from payments where project_id = :p and ts >= :fortnight and ts < :now
        group by currency order by total desc, n desc"""), p)).all()
    connected = await db.scalar(text("select max(ts) from payments where project_id = :p"), p)
    sells: list[Row] = []
    buyers: list[Row] = []
    if not connected:
        payments_n = 0
        numbers_money = [Number("Payments", "–", "Stripe is not connected yet"), Number("Paid", "–")]
    else:
        main = by_currency[0] if by_currency else None
        cur = main.currency if main else "usd"
        payments_n = sum(r.n for r in by_currency)
        others = " (plus other currencies)" if len([r for r in by_currency if r.n]) > 1 else ""
        paid_note = ""
        if main and has_last_week:
            diff = main.total - main.total_before
            paid_note = "about the same as last week" if diff == 0 else \
                f"{money(abs(diff), cur)} {'more' if diff > 0 else 'less'} than last week"
        numbers_money = [
            Number("Payments", f"{payments_n:,}",
                   compared(payments_n, sum(r.n_before for r in by_currency), has_last_week)),
            Number("Paid", money(main.total if main else 0, cur), paid_note + others),
        ]
        sells = [Row(r.product, r.n, amount=money(r.total, cur)) for r in await db.execute(text("""
            select product, count(*) n, sum(amount) total from payments
            where project_id = :p and ts >= :week and ts < :now and currency = :cur
            group by product order by total desc, product limit 6"""), {**p, "cur": cur})]
        if project.attribution_enabled:
            buyers = [Row(r.source, r.n, amount=money(r.total, cur)) for r in await db.execute(text("""
                select source, count(*) n, sum(amount) total from payments
                where project_id = :p and ts >= :week and ts < :now and currency = :cur and source is not null
                group by source order by total desc, source limit 6"""), {**p, "cur": cur})]

    numbers = [
        Number("Visitors", f"{visitors:,}", compared(visitors, visitors_before, has_last_week)),
        Number("Page views", f"{views:,}", compared(views, views_before, has_last_week)),
        *numbers_money,
        Number("Saw an error", f"{hit:,}", f"of {plural(visitors, 'visitor')}"),
    ]

    pages = [Row(r.path, r.v) for r in await db.execute(text("""
        select path, count(distinct visitor) v from pageviews
        where project_id = :p and ts >= :week and ts < :now
        group by path order by v desc, path limit 6"""), p)]

    # A visitor's source is where their first page view of the day came from.
    source_rows = (await db.execute(text("""
        select coalesce(source, 'Typed the address or unknown') name,
               bool_or(source_kind = 'ai') ai, count(*) v
        from (
            select distinct on (visitor) source, source_kind from pageviews
            where project_id = :p and ts >= :week and ts < :now
            order by visitor, ts
        ) first_views
        group by 1 order by v desc, name"""), p)).all()
    sources = [Row(r.name, r.v, "AI assistant" if r.ai else "") for r in source_rows[:5]]
    rest = sum(r.v for r in source_rows[5:])
    if rest:
        sources.append(Row("Everything else", rest))

    happened = [(day(i.started_at), past_tense(i)) for i in await db.scalars(
        select(Incident).where(Incident.project_id == project.id, Incident.ended_at.is_not(None),
                               Incident.level == "broken", Incident.started_at >= now - 30 * DAY)
        .order_by(Incident.started_at.desc()).limit(10))]

    coming_up = []
    if project.domain_expires_at and dt.timedelta(0) < project.domain_expires_at - now <= 90 * DAY:
        coming_up.append(f"Your domain needs renewing in {plural((project.domain_expires_at - now).days, 'day')}.")

    # Versions: what is behind, deprecated further down the tree, or has a
    # lesser security problem. None of these is an alert; they are listed.
    deps = (await db.scalars(select(Dependency).where(Dependency.project_id == project.id)
                             .order_by(Dependency.name))).all()
    package_notes: list[str] = []
    for d in deps:
        if patch_behind(d):
            name = PRODUCTS.get((d.ecosystem, d.name), (None, d.name))[1]
            line = f"{name} {d.version} is behind {d.latest}, the newest release of the same line."
            coming_up.append(line)
            package_notes.append(line)
    behind = sorted((d for d in deps if d.direct and majors_behind(d)), key=lambda d: (-majors_behind(d), d.name))
    if behind:
        named = ", ".join(f"{d.name} {d.version} → {d.latest}" for d in behind[:4])
        more = f" and {len(behind) - 4} more" if len(behind) > 4 else ""
        coming_up.append(f"{plural(len(behind), 'package')} {'is' if len(behind) == 1 else 'are'} "
                         f"a major version behind: {named}{more}.")
        package_notes += [f"{d.name} {d.version} is a major version behind ({d.latest} is the newest)." for d in behind]
    indirect = [d for d in deps if d.deprecated and not d.direct]
    if indirect:
        coming_up.append(f"{plural(len(indirect), 'package')} your app pulls in indirectly "
                         f"{'is' if len(indirect) == 1 else 'are'} deprecated by {'its' if len(indirect) == 1 else 'their'} authors.")
        package_notes += [f"{d.name} {d.version} (pulled in indirectly) is deprecated: {d.deprecated}" for d in indirect[:10]]
    lesser = [(d, issue) for d in deps for issue in (d.issues or [])]
    if lesser:
        coming_up.append(f"{plural(len(lesser), 'other known security problem')} in your packages, "
                         "not rated serious or with no fix yet.")
        package_notes += [f"{d.name} {d.version}: {issue}" for d, issue in lesser[:15]]

    work = [(job.name, f"checked in {ago(job.last_ping_at, now)}") for job in await db.scalars(
        select(Job).where(Job.project_id == project.id).order_by(Job.name))]
    for q in await db.execute(text("""
        select distinct on (name) name, ts, waiting, coalesce(oldest_seconds, max_wait_seconds) wait
        from queue_reports where project_id = :p and ts >= :day order by name, ts desc"""), p):
        if q.waiting:
            state = f"{plural(q.waiting, 'job')} waiting" + (f", the oldest for {lasted(q.wait)}" if q.wait else "")
        elif q.waiting == 0:
            state = "nothing waiting"
        else:
            state = f"jobs wait up to {lasted(q.wait)}" if q.wait and q.wait >= 60 else "jobs start straight away"
        work.append((f"{q.name} queue", f"{state}, as of {ago(q.ts, now)}"))
    has_logs = await db.scalar(text("select exists (select 1 from log_lines where project_id = :p)"), p)

    checks = await db.scalar(text(
        "select count(*) from site_checks where project_id = :p and ts >= :week and ts < :now"), p)
    if last_check is not None:
        proof = f"Checked {ago(last_check.ts, now)}."
    elif last_heard is not None:
        proof = f"Last heard from your site {ago(last_heard, now)}."
    else:
        proof = "Nothing has arrived from your site's pages yet."

    return Summary(project.domain, status, proof, numbers, pages, sources, sells, bool(connected),
                   f"Last payment: {ago(connected, now)}." if connected else "", problems, happened, coming_up,
                   await first_suggestion(db, project), work, package_notes, has_logs, visitors, payments_n, checks,
                   buyers)
