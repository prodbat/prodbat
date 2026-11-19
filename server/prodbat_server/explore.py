"""The views for engineers, behind the plain page: errors grouped into issues,
logs, metrics over time, uptime, jobs and queues, and packages. The plain page
stays the default; these are the tabs a Sentry or Datadog user looks for."""

import datetime as dt

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select, text

from .api import LEVELS, find_logs
from .charts import RANGES, bars, counts, spark, window
from . import dependencies
from .models import Incident, Job, QueueReport
from .page import PRIVATE, project_by_token, templates
from .tasks import path_value, where_value
from .words import money

router = APIRouter()
SOURCES = {"browser": "In the browser", "server": "On the server", "request": "Failed requests"}
REQUEST_KEY = "left(md5(method || ' ' || route || ' ' || status::text), 16)"


def view(request: Request, name: str, project, token: str, tab: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {
        "domain": project.domain, "token": token, "tab": tab, "ranges": list(RANGES), **context}, headers=PRIVATE)


def ago(when: dt.datetime | None, now: dt.datetime) -> str:
    if when is None:
        return "never"
    s = (now - when).total_seconds()
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if s >= size:
            return f"{int(s // size)}{unit} ago"
    return "just now"


# ---- errors, grouped into issues ------------------------------------------------

async def issues(db, project, w, source: str, q: str) -> list[dict]:
    like = {"q": f"%{q}%"} if q else {}
    out = []
    if source in ("all", "browser"):
        rows = await db.execute(text(
            "select fingerprint, max(message), max(file), max(line), count(*), count(distinct visitor), min(ts), "
            "max(ts), max(path) from browser_errors where project_id = :pid and ts >= :start"
            + (" and message ilike :q" if q else "") + " group by fingerprint"),
            {"pid": project.id, "start": w.start, **like})
        for fp, message, file, line, n, users, first, last, path in rows:
            out.append({"source": "browser", "key": fp, "title": message, "where": f"{file}:{line}" if file else path,
                        "count": n, "users": users, "first": first, "last": last})
    if source in ("all", "server"):
        rows = await db.execute(text(
            'select fingerprint, max(kind), max(message), max(route), max("where"), count(*), min(ts), max(ts) '
            "from server_errors where project_id = :pid and ts >= :start"
            + (" and (message ilike :q or kind ilike :q or route ilike :q)" if q else "") + " group by fingerprint"),
            {"pid": project.id, "start": w.start, **like})
        for fp, kind, message, route, where, n, first, last in rows:
            out.append({"source": "server", "key": fp, "title": f"{kind}: {message}" if message else kind,
                        "where": where or route, "count": n, "users": None, "first": first, "last": last})
    if source in ("all", "request"):
        rows = await db.execute(text(
            f"select {REQUEST_KEY}, method, route, status, count(*), count(distinct visitor), min(ts), max(ts) "
            "from failed_requests where project_id = :pid and ts >= :start"
            + (" and route ilike :q" if q else "") + " group by method, route, status"),
            {"pid": project.id, "start": w.start, **like})
        for key, method, route, status, n, users, first, last in rows:
            out.append({"source": "request", "key": key, "title": f"{method} {route} answered {status or 'nothing'}",
                        "where": route, "count": n, "users": users, "first": first, "last": last})
    return out


async def issue_series(db, project, w, item: dict) -> list[float]:
    table, where = {"browser": ("browser_errors", "and fingerprint = :key"),
                    "server": ("server_errors", "and fingerprint = :key"),
                    "request": ("failed_requests", f"and {REQUEST_KEY} = :key")}[item["source"]]
    return await counts(db, w, table, project.id, where, {"key": item["key"]})


@router.get("/d/{token}/errors", response_class=HTMLResponse)
async def errors(request: Request, token: str, range: str = "24h", source: str = "all", q: str = "",
                 sort: str = "last"):
    w = window(range)
    source = source if source in SOURCES or source == "all" else "all"
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        found = await issues(db, project, w, source, q.strip())
        found.sort(key=lambda i: i["count"] if sort == "count" else i["last"], reverse=True)
        for item in found[:100]:
            item["spark"] = spark(await issue_series(db, project, w, item))
        chart = bars([
            ("Browser errors", await counts(db, w, "browser_errors", project.id), "red"),
            ("Server errors", await counts(db, w, "server_errors", project.id), "amber"),
            ("Failed requests", await counts(db, w, "failed_requests", project.id), "ink"),
        ], w)
    now = dt.datetime.now(dt.UTC)
    for item in found:
        item["first_ago"], item["last_ago"] = ago(item["first"], now), ago(item["last"], now)
    return view(request, "errors.html", project, token, "errors", issues=found[:100], total=len(found), chart=chart,
                range=w.key, source=source, q=q, sort=sort, sources=SOURCES)


@router.get("/d/{token}/errors/{source}/{key}", response_class=HTMLResponse)
async def issue(request: Request, token: str, source: str, key: str, range: str = "7d"):
    if source not in SOURCES:
        raise HTTPException(404, "no such issue")
    w = window(range)
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        params = {"pid": project.id, "key": key}
        if source == "browser":
            rows = (await db.execute(text(
                "select ts, path, visitor, message, file, line, col, stack from browser_errors "
                "where project_id = :pid and fingerprint = :key order by ts desc limit 50"), params)).all()
            events = [{"ts": r[0], "at": r[1], "who": r[2][:8]} for r in rows]
            latest = rows[0] if rows else None
            detail = latest and {"title": latest[3], "where": f"{latest[4]}:{latest[5]}:{latest[6]}",
                                 "trace": latest[7], "route": latest[1]}
        elif source == "server":
            rows = (await db.execute(text(
                'select ts, route, status, kind, message, "where", trace from server_errors '
                "where project_id = :pid and fingerprint = :key order by ts desc limit 50"), params)).all()
            events = [{"ts": r[0], "at": r[1], "who": str(r[2] or "")} for r in rows]
            latest = rows[0] if rows else None
            detail = latest and {"title": f"{latest[3]}: {latest[4]}" if latest[4] else latest[3],
                                 "where": latest[5], "trace": latest[6], "route": latest[1]}
        else:
            rows = (await db.execute(text(
                f"select ts, path, visitor, method, route, status from failed_requests "
                f"where project_id = :pid and {REQUEST_KEY} = :key order by ts desc limit 50"), params)).all()
            events = [{"ts": r[0], "at": r[1], "who": r[2][:8]} for r in rows]
            latest = rows[0] if rows else None
            detail = latest and {"title": f"{latest[3]} {latest[4]} answered {latest[5] or 'nothing'}",
                                 "where": latest[4], "trace": "", "route": f"{latest[3]} {latest[4]}"}
        if detail is None:
            raise HTTPException(404, "no such issue")
        item = {"source": source, "key": key}
        chart = bars([(SOURCES[source], await issue_series(db, project, w, item), "red")], w)
        total = sum(await issue_series(db, project, w, item))
        last = events[0]["ts"]
        nearby = await find_logs(db, project, since=last - dt.timedelta(minutes=2), until=last + dt.timedelta(minutes=1),
                                 limit=60)
    # Only checked values go into the line for the agent; the error text itself is data.
    place = where_value(detail["where"] or "") or path_value(detail["route"] or "")
    prompt = (f"Prodbat shows a repeated error on {project.domain}" + (f" at {place}" if place else "") +
              ". Run `npx prodbat status` in this project for the details, check them against the code before "
              "changing anything, then fix it.")
    now = dt.datetime.now(dt.UTC)
    return view(request, "issue.html", project, token, "errors", detail=detail, events=events, chart=chart,
                total=int(total), range=w.key, source=source, source_label=SOURCES[source], key=key,
                first_ago=ago(events[-1]["ts"], now), last_ago=ago(last, now), logs=nearby, prompt=prompt)


# ---- logs -------------------------------------------------------------------------

@router.get("/d/{token}/logs", response_class=HTMLResponse)
async def logs(request: Request, token: str, q: str = "", level: str = "", range: str = "24h",
               before: str | None = None, live: str = ""):
    w = window(range)
    level = level if level in LEVELS else ""
    until = None
    if before:
        try:
            until = dt.datetime.fromisoformat(before)
        except ValueError:
            raise HTTPException(400, "before must be a time") from None
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        lines = await find_logs(db, project, q.strip() or None, level or None, since=w.start, until=until, limit=500)
        where = " and message ilike :q" if q.strip() else ""
        params = {"q": f"%{q.strip()}%"} if q.strip() else {}
        series = [(name.capitalize(), await counts(db, w, "log_lines", project.id, where + " and level = :lv",
                                                    {**params, "lv": name}), color)
                  for name, color in (("error", "red"), ("warning", "amber"), ("info", "grey"), ("debug", "mint"))
                  if not level or LEVELS[name] >= LEVELS[level]]
        chart = bars(series, w)
        keep = project.log_keep_days or request.app.state.settings.log_keep_days
    older = lines[0].ts.isoformat() if len(lines) == 500 else None
    return view(request, "logs.html", project, token, "logs", lines=lines[::-1], q=q, level=level, levels=list(LEVELS),
                keep=keep, chart=chart, range=w.key, older=older, live=bool(live))


# ---- metrics -----------------------------------------------------------------------

@router.get("/d/{token}/metrics", response_class=HTMLResponse)
async def metrics(request: Request, token: str, range: str = "24h"):
    w = window(range)
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        pid = project.id
        views = await counts(db, w, "pageviews", pid)
        visitors = await counts(db, w, "pageviews", pid, value="count(distinct visitor)")
        browser = await counts(db, w, "browser_errors", pid)
        server = await counts(db, w, "server_errors", pid)
        failed = await counts(db, w, "failed_requests", pid)
        fives = await counts(db, w, "server_errors", pid, "and status >= 500")
        ms = await counts(db, w, "site_checks", pid, "and ms is not null", value="avg(ms)")
        slowest = await counts(db, w, "site_checks", pid, "and ms is not null", value="max(ms)")
        checks = await counts(db, w, "site_checks", pid)
        ok = await counts(db, w, "site_checks", pid, "and ok is not false")
        paid = await counts(db, w, "payments", pid)
        amount = await counts(db, w, "payments", pid, value="sum(amount)")
        by_source = (await db.execute(text(
            "select coalesce(source, 'Not traced'), currency, count(*), sum(amount) from payments "
            "where project_id = :pid and ts >= :start group by 1, 2 order by 4 desc limit 20"),
            {"pid": pid, "start": w.start})).all() if project.attribution_enabled else []
        unique = await db.scalar(text(
            "select count(distinct visitor) from pageviews where project_id = :pid and ts >= :start"),
            {"pid": pid, "start": w.start})
        overall_ms = await db.scalar(text(
            "select avg(ms) from site_checks where project_id = :pid and ts >= :start and ms is not null"),
            {"pid": pid, "start": w.start})
    up = [100 * o / c if c else 0 for o, c in zip(ok, checks)]
    down = [100 - u if c else 0 for u, c in zip(up, checks)]
    stats = [
        ("Page views", f"{sum(views):,.0f}"),
        ("Visitors", f"{unique or 0:,}"),
        ("Errors", f"{sum(browser) + sum(server) + sum(failed):,.0f}"),
        ("Server 5xx", f"{sum(fives):,.0f}"),
        ("Uptime", f"{100 * sum(ok) / sum(checks):.2f}%" if sum(checks) else "–"),
        ("Avg response", f"{overall_ms:,.0f} ms" if overall_ms is not None else "–"),
        ("Payments", f"{sum(paid):,.0f}"),
    ]
    charts = [
        ("Page views", bars([("Page views", views, "ink")], w)),
        ("Visitors", bars([("Visitors", visitors, "blue")], w)),
        ("Errors", bars([("Browser", browser, "red"), ("Server", server, "amber"), ("Failed requests", failed, "ink")], w)),
        ("Response time from outside", bars([("Average", ms, "green")], w, unit=" ms")),
        ("Slowest response", bars([("Slowest", slowest, "amber")], w, unit=" ms")),
        ("Uptime", bars([("Up", up, "green"), ("Down", down, "red")], w, unit="%", max_value=100)),
        ("Payments", bars([("Payments", paid, "mint")], w)),
        ("Revenue", bars([("Amount", [a / 100 for a in amount], "green")], w)),
    ]
    revenue = [{"source": src, "count": n, "amount": money(total, cur)} for src, cur, n, total in by_source]
    return view(request, "metrics.html", project, token, "metrics", stats=stats, charts=charts, range=w.key,
                revenue=revenue)


# ---- uptime -------------------------------------------------------------------------

@router.get("/d/{token}/uptime", response_class=HTMLResponse)
async def uptime(request: Request, token: str, range: str = "24h"):
    w = window(range)
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        rows = (await db.execute(text(
            "select path, coalesce(region, ''), count(*), count(*) filter (where ok is not false), avg(ms), "
            "percentile_cont(0.95) within group (order by ms), max(ts) from site_checks "
            "where project_id = :pid and ts >= :start group by path, coalesce(region, '') order by path, 2"),
            {"pid": project.id, "start": w.start})).all()
        pages = []
        for path, region, n, ok, avg, p95, last in rows:
            recent = (await db.execute(text(
                "select ok, status, ms, ts, error from site_checks where project_id = :pid and path = :path "
                "and coalesce(region, '') = :region order by ts desc limit 60"),
                {"pid": project.id, "path": path, "region": region})).all()
            pages.append({"path": path, "region": region, "checks": n, "uptime": 100 * ok / n if n else 0,
                          "avg": avg, "p95": p95, "recent": recent[::-1], "last": recent[0] if recent else None})
        history = (await db.scalars(select(Incident).where(Incident.project_id == project.id)
                                    .order_by(Incident.started_at.desc()).limit(30))).all()
    now = dt.datetime.now(dt.UTC)
    incidents = [{"title": i.title, "level": i.level, "started": i.started_at,
                  "lasted": ago(i.started_at, i.ended_at or now).replace(" ago", "") if i.ended_at else "ongoing"}
                 for i in history]
    return view(request, "uptime.html", project, token, "uptime", pages=pages, incidents=incidents, range=w.key,
                every=project.check_every_seconds)


# ---- jobs and queues --------------------------------------------------------------------

@router.get("/d/{token}/jobs", response_class=HTMLResponse)
async def jobs(request: Request, token: str, range: str = "24h"):
    w = window(range)
    now = dt.datetime.now(dt.UTC)
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        scheduled = (await db.scalars(select(Job).where(Job.project_id == project.id).order_by(Job.name))).all()
        names = (await db.execute(select(QueueReport.system, QueueReport.name).where(
            QueueReport.project_id == project.id).distinct())).all()
        queues = []
        for system, name in names:
            latest = await db.scalar(select(QueueReport).where(
                QueueReport.project_id == project.id, QueueReport.system == system, QueueReport.name == name)
                .order_by(QueueReport.ts.desc()).limit(1))
            where = "and system = :system and name = :name"
            p = {"system": system, "name": name}
            queues.append({"system": system, "name": name, "latest": latest, "chart": bars([
                ("Waiting", await counts(db, w, "queue_reports", project.id, where, p, value="max(waiting)"), "ink"),
            ], w, height=70), "throughput": bars([
                ("Done", await counts(db, w, "queue_reports", project.id, where, p, value="sum(done)"), "green"),
                ("Failed", await counts(db, w, "queue_reports", project.id, where, p, value="sum(failed)"), "red"),
            ], w, height=70)})
    rows = []
    for job in scheduled:
        late = job.last_ping_at is None or now - job.last_ping_at > dt.timedelta(seconds=job.every_seconds * 1.5)
        rows.append({"name": job.name, "every": job.every_seconds, "last": ago(job.last_ping_at, now), "late": late})
    return view(request, "jobs.html", project, token, "jobs", jobs=rows, queues=queues, range=w.key)


# ---- packages -------------------------------------------------------------------------

@router.get("/d/{token}/packages", response_class=HTMLResponse)
async def packages(request: Request, token: str, show: str = "all"):
    """The same table as the page, with every indirect package listed too."""
    show = "problems" if show == "problems" else "all"
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        deps = await dependencies.table(db, project, every=True, problems_only=show == "problems")
    return view(request, "packages.html", project, token, "packages", deps=deps, show=show)
