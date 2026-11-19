"""Outside checks: load each site every few minutes and record when it is
down; once a day, look up when its certificate and domain run out."""

import asyncio
import datetime as dt
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass

import httpx
from sqlalchemy import select

from .config import Settings
from .flows import SAY, flow_for
from .incidents import report, resolve
from .models import Incident, Project, SiteCheck
from .words import day, plural

CHECK_EVERY = dt.timedelta(minutes=5)
TIMEOUT = 15.0
# A failed check is tried once more before the site is called down.
RECHECK_AFTER = 20.0
USER_AGENT = "Prodbat/0.1 (site check; +https://github.com/prodbat/prodbat)"
# The site answered, but only to say it will not talk to us (a login wall,
# a bot challenge, a rate limit). That is not evidence it is down.
CANNOT_TELL = {401, 403, 429}
CERT_WARN = dt.timedelta(days=14)
DOMAIN_WARN = dt.timedelta(days=30)


@dataclass
class Result:
    ok: bool | None  # None: cannot tell
    status: int | None = None
    ms: int | None = None
    # Reads after the site's name: "took longer than 15 seconds to answer".
    error: str | None = None
    body: str = ""
    headers: dict | None = None
    # Where we ended up after redirects.
    url: str = ""
    # Each Set-Cookie header of the last answer, kept apart (dict(headers) joins them).
    cookies: list[str] | None = None


def is_public_host(host: str) -> bool:
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return True  # does not resolve: let the request fail with the real reason
    return all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)


async def _refuse_private_addresses(request: httpx.Request) -> None:
    # Sites are named by strangers, so never let one point us at this
    # server's own network. Runs for every redirect hop too. (A name can
    # still change between this lookup and the connection.)
    if not await asyncio.to_thread(is_public_host, request.url.host):
        raise httpx.ConnectError("refusing to load a private address", request=request)


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers={"user-agent": USER_AGENT},
                             event_hooks={"request": [_refuse_private_addresses]})


def describe(exc: httpx.HTTPError) -> str:
    text = str(exc).lower()
    if isinstance(exc, httpx.TimeoutException):
        return f"took longer than {TIMEOUT:.0f} seconds to answer"
    if "certificate" in text:
        return "has a secure connection certificate that browsers will not accept"
    if "name or service not known" in text or "nodename nor servname" in text or "name resolution" in text:
        return "has a domain name that does not point anywhere"
    return "could not be reached"


async def check_site(client: httpx.AsyncClient, domain: str, path: str = "/") -> Result:
    started = time.monotonic()
    try:
        response = await client.get(f"https://{domain}{path}")
    except httpx.HTTPError as exc:
        return Result(False, error=describe(exc))
    ms = int((time.monotonic() - started) * 1000)
    status = response.status_code
    extra = {"headers": dict(response.headers), "url": str(response.url),
             "cookies": response.headers.get_list("set-cookie")}
    if status in CANNOT_TELL:
        return Result(None, status, ms, **extra)
    if response.is_success:
        return Result(True, status, ms, body=response.text[:500_000], **extra)
    return Result(False, status, ms, f"answered with an error ({status})", **extra)


async def other_regions(db, project: Project, settings: Settings, now: dt.datetime) -> dict[str, bool]:
    """For a project checked from several regions: whether each other region's
    latest check of the home page, from the last few minutes, loaded it."""
    if not project.check_regions:
        return {}
    recent = now - dt.timedelta(seconds=2 * (project.check_every_seconds or 300) + 120)
    rows = await db.execute(select(SiteCheck.region, SiteCheck.ok).where(
        SiteCheck.project_id == project.id, SiteCheck.path == "/", SiteCheck.ts >= recent,
        SiteCheck.region.is_not(None), SiteCheck.region != (settings.region or "")).order_by(SiteCheck.ts))
    latest: dict[str, bool] = {}
    for region, ok in rows:
        latest[region] = ok is not False
    return latest


async def check_project(sessions, client, settings: Settings, project_id, recheck_after: float) -> Result:
    async with sessions() as db:
        domain = (await db.get(Project, project_id)).domain
    result = await check_site(client, domain)
    if result.ok is False:
        await asyncio.sleep(recheck_after)
        result = await check_site(client, domain)

    now = dt.datetime.now(dt.UTC)
    async with sessions() as db:
        project = await db.get(Project, project_id)
        db.add(SiteCheck(project_id=project_id, ts=now, ok=result.ok, status=result.status, ms=result.ms,
                         error=result.error, region=settings.region or None))
        elsewhere = await other_regions(db, project, settings, now) if result.ok is False else {}
        if result.ok is False and any(elsewhere.values()):
            pass  # another region loaded it just now: a problem on the way from here, not the site being down
        elif result.ok is False:
            places = "from outside" if not elsewhere else f"from {len(elsewhere) + 1} places"
            await report(db, project, settings, kind="down", level="broken", title="Your site is not loading",
                         what=f"{domain} {result.error}. We tried twice {places}.",
                         who="Everyone who visits.", started_at=now,
                         todo="Check your host's status page and your last deploy, "
                              "or paste the line below into your coding agent.")
        elif result.ok:
            down = await db.scalar(select(Incident).where(
                Incident.project_id == project_id, Incident.kind == "down", Incident.ended_at.is_(None)))
            if down is not None:
                resolve(db, project, down, settings, now, f"Your site is loading again: we loaded it from outside "
                                                         f"in {result.ms / 1000:.1f} seconds.")
        pages = [p for p in (project.pages or []) if p != "/"][:5] if result.ok else []
        await db.commit()

    # Other pages that matter (sign-in, pricing), only while the home page loads:
    # a site that is down altogether is one problem, not several.
    for path in pages:
        page = await check_site(client, domain, path)
        if page.ok is False:
            await asyncio.sleep(recheck_after)
            page = await check_site(client, domain, path)
        async with sessions() as db:
            project = await db.get(Project, project_id)
            db.add(SiteCheck(project_id=project_id, ts=now, path=path, ok=page.ok, status=page.status, ms=page.ms,
                             error=page.error, region=settings.region or None))
            flow = flow_for(None, path)
            if page.ok is False:
                await report(db, project, settings, kind="page", key=path, level="broken",
                             title=f"Your {SAY[flow][1]} page is not loading" if flow else f"{path} is not loading",
                             what=f"{domain}{path} {page.error}. The home page loads. We tried twice from outside.",
                             who="Everyone who opens that page.", started_at=now, data={"path": path})
            elif page.ok:
                broken = await db.scalar(select(Incident).where(
                    Incident.project_id == project_id, Incident.kind == "page", Incident.key == path,
                    Incident.ended_at.is_(None)))
                if broken is not None:
                    resolve(db, project, broken, settings, now, f"The page is loading again: we loaded it from outside "
                                                           f"in {page.ms / 1000:.1f} seconds.")
            await db.commit()
    return result


async def run_due_checks(sessions, client, settings: Settings, recheck_after: float = RECHECK_AFTER) -> int:
    """Check every site that is due. Returns how many were checked.
    Several workers can run at once; each claims its sites with SKIP LOCKED."""
    now = dt.datetime.now(dt.UTC)
    async with sessions() as db:
        due = (await db.scalars(
            select(Project).where(Project.next_check_at <= now, Project.paused_at.is_(None))
            .order_by(Project.next_check_at).limit(50).with_for_update(skip_locked=True))).all()
        for project in due:
            project.next_check_at = now + dt.timedelta(seconds=project.check_every_seconds or CHECK_EVERY.total_seconds())
        claimed = [project.id for project in due]
        await db.commit()
    await asyncio.gather(*(check_project(sessions, client, settings, pid, recheck_after) for pid in claimed))
    return len(claimed)


def certificate_expiry(domain: str) -> dt.datetime | None:
    try:
        with socket.create_connection((domain, 443), timeout=10) as sock, \
                ssl.create_default_context().wrap_socket(sock, server_hostname=domain) as tls:
            not_after = tls.getpeercert()["notAfter"]
    except (OSError, KeyError):
        return None
    return dt.datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), dt.UTC)


async def domain_expiry(client: httpx.AsyncClient, domain: str) -> dt.datetime | None:
    """From the public registration record (RDAP). Tries app.example.com,
    then example.com."""
    labels = domain.split(".")
    for start in range(0, max(1, len(labels) - 1))[:3]:
        try:
            response = await client.get("https://rdap.org/domain/" + ".".join(labels[start:]))
            if response.status_code != 200:
                continue
            for event in response.json().get("events", []):
                if event.get("eventAction") == "expiration":
                    return dt.datetime.fromisoformat(event["eventDate"].replace("Z", "+00:00"))
        except (httpx.HTTPError, ValueError, KeyError):
            continue
    return None


async def apply_expiry(db, project: Project, settings: Settings, now: dt.datetime) -> None:
    """Open or close the "runs out soon" problems from the stored dates."""
    for kind, expires, warn, title, what, todo in [
        ("cert", project.cert_expires_at, CERT_WARN, "Your secure connection certificate runs out",
         "Most hosts renew it by themselves about a month ahead, so this renewal may be stuck. "
         "When it runs out, browsers will show visitors a warning instead of your site.",
         "Look for a certificate or domain warning in your host's dashboard."),
        ("domain", project.domain_expires_at, DOMAIN_WARN, "Your domain needs renewing",
         "If it lapses, your site and any email on the domain stop working.",
         "Renew it where you bought the domain, and switch on automatic renewal."),
    ]:
        open_ = await db.scalar(select(Incident).where(
            Incident.project_id == project.id, Incident.kind == kind, Incident.ended_at.is_(None)))
        if expires is not None and expires - now <= warn:
            days = max(0, (expires - now).days)
            await report(db, project, settings, kind=kind, level="look",
                         title=f"{title} in {plural(days, 'day')}",
                         what=f"It runs out on {day(expires)} {expires.year}. {what}",
                         who="Nobody yet.", todo=todo, started_at=now)
        elif open_ is not None:
            resolve(db, project, open_, settings, now)


async def refresh_expiry(sessions, client, settings: Settings, certificate_expiry=certificate_expiry) -> None:
    """Once a day per site."""
    now = dt.datetime.now(dt.UTC)
    async with sessions() as db:
        stale = (await db.scalars(select(Project).where(Project.paused_at.is_(None)).where(
            (Project.expiry_checked_at.is_(None)) | (Project.expiry_checked_at < now - dt.timedelta(days=1)))
            .limit(20))).all()
        for project in stale:
            project.expiry_checked_at = now
            project.cert_expires_at = await asyncio.to_thread(certificate_expiry, project.domain) \
                or project.cert_expires_at
            project.domain_expires_at = await domain_expiry(client, project.domain) or project.domain_expires_at
            await apply_expiry(db, project, settings, now)
            await db.commit()
