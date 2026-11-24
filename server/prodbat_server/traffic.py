"""A site's normal traffic, learned hour by hour, to tell a quiet night from an outage.

Each completed hour's visits are saved in visit_hours (`roll_up`). From a
rolling 4 weeks of those, `profile` gives the visits a site normally gets in
each of the 168 hours of the week. Hours are UTC, so each site's own nights
show up wherever its visitors sleep, without asking for a time zone.

A day of history is enough to start. Each hour of the week leans on broader
patterns, first the same hour on every day, then the site's overall rate, by
about PRIOR_VISITS visits' worth. So a busy site's first day already shows
its nights, and a small site leans on its overall rate for longer, which does
no harm: at three visits a day a quiet night never adds up to many expected.
Once an hour has three or more readings its single busiest one is left out,
so one launch day does not make every later Tuesday look slow.

`silence_chance` adds up the visits normally expected across exactly the
hours a silence covered, and gives the chance of none of them arriving.

Nothing alerts from this yet. `replay` runs it beside the rules in detect.py
over real history, so its thresholds are set from what it would have said.
"""

import bisect
import datetime as dt
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Incident, Project

HOUR = dt.timedelta(hours=1)
# Visits, not page views: a visitor's page views are one visit until they have been away this long.
VISIT_GAP = dt.timedelta(minutes=30)
BASELINE = dt.timedelta(days=28)
MIN_HISTORY = dt.timedelta(days=1)
# How strongly each hour leans on the broader pattern, in visits. A first guess; see replay.
PRIOR_VISITS = 1.0
TRIM_FROM = 3
# The same bars as detect.py's went-quiet rule, and a silence shorter than this never counts.
LOOK_CHANCE = 1e-3
BROKEN_CHANCE = 1e-5
MIN_SILENCE = dt.timedelta(minutes=10)
# visit_hours is recomputed this far back each time, for page views that arrive late; kept this long.
ROLL_BACK = dt.timedelta(hours=3)
KEEP = dt.timedelta(days=35)


def floor_hour(t: dt.datetime) -> dt.datetime:
    return t.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)


def slot(t: dt.datetime) -> int:
    """The hour of the week, 0 for Monday 00:00 UTC."""
    t = t.astimezone(dt.UTC)
    return t.weekday() * 24 + t.hour


async def roll_up(db: AsyncSession, project_id, now: dt.datetime, since: dt.datetime | None = None) -> None:
    """Count the visits that began in each completed hour since `since`."""
    until = floor_hour(now)
    await db.execute(text("""
        insert into visit_hours (project_id, hour, visits)
        select :p, date_trunc('hour', ts, 'UTC'), count(*) from (
            select ts, lag(ts) over (partition by visitor order by ts) prev from pageviews
            where project_id = :p and ts >= :since - :gap and ts < :until) v
        where ts >= :since and (prev is null or ts - prev > :gap)
        group by 2
        on conflict (project_id, hour) do update set visits = excluded.visits"""),
        {"p": project_id, "since": since or until - ROLL_BACK, "until": until, "gap": VISIT_GAP})


async def roll_up_all(sessions, now: dt.datetime | None = None) -> None:
    now = now or dt.datetime.now(dt.UTC)
    async with sessions() as db:
        for project_id in (await db.scalars(select(Project.id).where(Project.paused_at.is_(None)))).all():
            await roll_up(db, project_id, now)
        await db.execute(text("delete from visit_hours where hour < :cutoff"), {"cutoff": now - KEEP})
        await db.commit()


def profile(counts: dict[dt.datetime, int], first: dt.datetime, end: dt.datetime,
            excluded: set[dt.datetime] = frozenset()) -> list[float] | None:
    """Normal visits for each hour of the week, from the completed hours between
    `first` (the site's first visit) and `end`, over at most BASELINE. Hours in
    `excluded` (a past outage) are left out. None with under a day of history."""
    end = floor_hour(end)
    hour = max(floor_hour(first), end - BASELINE)
    by_day, by_week = defaultdict(list), defaultdict(list)
    seen = total = 0
    while hour < end:
        if hour not in excluded:
            n = counts.get(hour, 0)
            by_day[hour.hour].append(n)
            by_week[slot(hour)].append(n)
            seen += 1
            total += n
        hour += HOUR
    if seen < MIN_HISTORY / HOUR:
        return None

    def trimmed(readings: list[int]) -> tuple[int, int]:
        if len(readings) >= TRIM_FROM:
            return sum(readings) - max(readings), len(readings) - 1
        return sum(readings), len(readings)

    overall = total / seen
    day = []
    for h in range(24):
        s, n = trimmed(by_day[h])
        lean = PRIOR_VISITS / overall if overall else 0.0  # in days
        day.append((s + lean * overall) / (n + lean) if n + lean else 0.0)
    week = []
    for s_ in range(168):
        s, n = trimmed(by_week[s_])
        parent = day[s_ % 24]
        lean = PRIOR_VISITS / parent if parent else 0.0  # in weeks
        week.append((s + lean * parent) / (n + lean) if n + lean else 0.0)
    return week


def expected(rates: list[float], start: dt.datetime, end: dt.datetime) -> float:
    """Visits normally seen between `start` and `end`."""
    total, t = 0.0, start
    while t < end:
        step = min(floor_hour(t) + HOUR, end)
        total += rates[slot(t)] * (step - t).total_seconds() / 3600
        t = step
    return total


def silence_chance(rates: list[float], last: dt.datetime, now: dt.datetime) -> tuple[float, float]:
    """The chance of no visit since `last`, and the visits normally expected in that time."""
    usual = expected(rates, last, now)
    return math.exp(-usual), usual


def level_for(rates: list[float] | None, last: dt.datetime | None, now: dt.datetime) -> str | None:
    if rates is None or last is None or now - last < MIN_SILENCE:
        return None
    chance, _ = silence_chance(rates, last, now)
    return "broken" if chance < BROKEN_CHANCE else "look" if chance < LOOK_CHANCE else None


async def excluded_hours(db: AsyncSession, project_id, since: dt.datetime, until: dt.datetime) -> set[dt.datetime]:
    """Hours inside a past outage of visits, which must not teach that silence is normal."""
    rows = (await db.execute(select(Incident.started_at, Incident.ended_at).where(
        Incident.project_id == project_id, Incident.kind == "quiet", Incident.key == "visitors",
        Incident.level == "broken", Incident.started_at < until,
        (Incident.ended_at.is_(None)) | (Incident.ended_at > since)))).all()
    hours = set()
    for start, end in rows:
        hour = floor_hour(max(start, since))
        while hour < min(end or until, until):
            hours.add(hour)
            hour += HOUR
    return hours


# Replay: what each rule would have said, every few minutes over real history.

@dataclass
class Episode:
    opened: dt.datetime
    quiet_since: dt.datetime
    level: str
    closed: dt.datetime | None = None
    usual: float | None = None  # visits normally expected over the silence, when it opened (hourly rule)
    also: list[str] = field(default_factory=list)  # other problems open at the time


@dataclass
class Replay:
    domain: str
    visits: int
    days: float
    rules: dict[str, list[Episode]]


class _Flat:
    """The even-rate rule of detect.went_quiet, over arrival times `events`."""

    def __init__(self, events: list[dt.datetime], longest_factor: float | None, min_events: int = 50):
        self.events, self.factor, self.min_events = events, longest_factor, min_events
        self.gaps: deque[tuple[int, float]] = deque()  # (index, gap after events[index]), largest first
        self.added = 0

    def level(self, now: dt.datetime, last: dt.datetime | None) -> str | None:
        lo = bisect.bisect_left(self.events, now - BASELINE)
        hi = bisect.bisect_right(self.events, now)
        n = hi - lo
        if n < self.min_events or last is None or last - self.events[lo] < dt.timedelta(days=3):
            return None
        while self.added < hi - 1:  # gaps between events lo..hi-1, kept as a running maximum
            gap = (self.events[self.added + 1] - self.events[self.added]).total_seconds()
            while self.gaps and self.gaps[-1][1] <= gap:
                self.gaps.pop()
            self.gaps.append((self.added, gap))
            self.added += 1
        while self.gaps and self.gaps[0][0] < lo:
            self.gaps.popleft()
        longest = self.gaps[0][1] if self.gaps else 0
        usual = (last - self.events[lo]).total_seconds() / (n - 1)
        silence = (now - last).total_seconds()
        chance = math.exp(-silence / usual) if usual > 0 else 0.0
        if chance >= LOOK_CHANCE or (self.factor is not None and silence <= self.factor * longest):
            return None
        return "broken" if chance < BROKEN_CHANCE else "look"


async def replay(db: AsyncSession, project: Project, start: dt.datetime, end: dt.datetime,
                 step: dt.timedelta = dt.timedelta(minutes=10)) -> Replay:
    """Walk from `start` to `end`, asking each rule at every `step` whether the
    site's visits have stopped, using only what was known at that moment."""
    rows = (await db.execute(text("""
        select ts, visitor from pageviews where project_id = :p and ts >= :since and ts < :until order by ts"""),
        {"p": project.id, "since": start - BASELINE - VISIT_GAP, "until": end})).all()
    views = [r.ts for r in rows]
    seen: dict[str, dt.datetime] = {}
    starts = []
    for ts, visitor in rows:
        if visitor not in seen or ts - seen[visitor] > VISIT_GAP:
            starts.append(ts)
        seen[visitor] = ts
    counts: dict[dt.datetime, int] = defaultdict(int)
    for ts in starts:
        counts[floor_hour(ts)] += 1
    others = (await db.execute(select(Incident).where(
        Incident.project_id == project.id, Incident.started_at < end,
        (Incident.ended_at.is_(None)) | (Incident.ended_at > start - BASELINE)))).scalars().all()

    flat = {"page views (before 23 Nov)": _Flat(views, None), "visits (now)": _Flat(starts, 1.5)}
    rules: dict[str, list[Episode]] = {name: [] for name in [*flat, "hourly pattern"]}
    outage: set[dt.datetime] = set()  # hours the hourly rule called broken, left out of its own baseline
    cached: tuple[dt.datetime, list[float] | None] | None = None

    def note(name: str, level: str | None, now: dt.datetime, last: dt.datetime | None, usual: float | None = None):
        episodes = rules[name]
        current = episodes[-1] if episodes and episodes[-1].closed is None else None
        if level and current is None:
            also = [f"T{i.id} {i.title}" for i in others
                    if i.started_at <= now and (i.ended_at is None or i.ended_at > last)]
            episodes.append(Episode(now, last, level, usual=usual, also=also))
        elif level and level == "broken":
            current.level = "broken"
        elif not level and current is not None:
            current.closed = now

    t = start
    while t <= end:
        i = bisect.bisect_right(views, t)
        last = views[i - 1] if i else None
        for name, rule in flat.items():
            note(name, rule.level(t, last), t, last)
        if starts and starts[0] <= t:
            hour = floor_hour(t)
            if cached is None or cached[0] != hour:
                cached = (hour, profile(counts, starts[0], hour, outage))
            level = level_for(cached[1], last, t)
            if level == "broken":
                outage.add(hour)
            note("hourly pattern", level, t, last,
                 silence_chance(cached[1], last, t)[1] if level and cached[1] else None)
        t += step

    in_range = bisect.bisect_left(starts, start)
    return Replay(project.domain, len(starts) - in_range, (end - start) / dt.timedelta(days=1), rules)


def describe(result: Replay, now: dt.datetime) -> str:
    from .words import lasted

    per_day = result.visits / result.days if result.days else 0
    lines = [f"{result.domain}: {result.visits:,} visits over {result.days:.0f} days ({per_day:,.1f} a day)"]
    for name, episodes in result.rules.items():
        lines.append(f"  {name}: {len(episodes)} {'alert' if len(episodes) == 1 else 'alerts'}")
        for e in episodes:
            quiet = lasted(((e.closed or now) - e.quiet_since).total_seconds())
            line = (f"    {e.opened:%a %d %b %H:%M} UTC  {e.level:<6}  quiet for {quiet}"
                    f"{'' if e.closed else ' (still)'}, alerted after {lasted((e.opened - e.quiet_since).total_seconds())}")
            if e.usual is not None:
                line += f", {e.usual:.1f} visits usual by then"
            if e.also:
                line += f"; also open: {'; '.join(e.also)}"
            lines.append(line)
    return "\n".join(lines)


async def replay_all(settings, days: int, domain: str | None) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(settings.database_url)
    now = dt.datetime.now(dt.UTC)
    totals: dict[str, int] = defaultdict(int)
    sites = 0
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        query = select(Project).order_by(Project.domain)
        if domain:
            query = query.where(Project.domain == domain)
        for project in (await db.scalars(query)).all():
            result = await replay(db, project, now - dt.timedelta(days=days), now)
            if not result.visits:
                continue
            sites += 1
            for name, episodes in result.rules.items():
                totals[name] += len(episodes)
            print(describe(result, now), "\n", flush=True)
        await db.rollback()  # read only
    await engine.dispose()
    print(f"{sites} sites with visits, last {days} days:")
    for name, n in totals.items():
        print(f"  {name}: {n} {'alert' if n == 1 else 'alerts'}")
