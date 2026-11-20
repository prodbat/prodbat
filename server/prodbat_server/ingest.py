"""The endpoint the browser script posts to.

The script sends with navigator.sendBeacon, which posts text/plain and
never reads the answer, so the body is parsed by hand and the status
codes exist for `prodbat verify` and for debugging.
"""

import datetime as dt
import hashlib
import re
import secrets
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from . import noise
from .models import BrowserError, DailySalt, FailedRequest, Pageview, Project, normalize_domain
from .sources import classify

router = APIRouter()

MAX_BODY = 8192
CORS = {"Access-Control-Allow-Origin": "*"}
BOT = re.compile(
    r"bot|crawl|spider|slurp|headless|lighthouse|pingdom|uptime|preview|curl|wget|python|httpx|okhttp|go-http",
    re.I,
)


class Event(BaseModel):
    k: str = Field(max_length=64)  # site key
    t: Literal["pv", "err", "req"]
    u: str = Field(max_length=2000)  # page URL
    r: str | None = Field(default=None, max_length=2000)  # referrer (pv)
    i: str | None = Field(default=None, max_length=16)  # reference for this page load (pv)
    # err
    m: str = Field(default="", max_length=300)
    f: str = Field(default="", max_length=500)
    l: int = Field(default=0, ge=0, le=10_000_000)  # noqa: E741
    c: int = Field(default=0, ge=0, le=10_000_000)
    s: str = Field(default="", max_length=2000)
    # req
    me: str = Field(default="GET", max_length=10)
    ru: str = Field(default="", max_length=500)
    st: int = Field(default=0, ge=0, le=599)


def client_ip(request: Request, proxy_hops: int) -> str:
    if proxy_hops > 0:
        # Each trusted proxy appends the address it saw, so the client is
        # proxy_hops entries from the right. Entries further left are
        # whatever the client chose to send.
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        if len(hops) >= proxy_hops:
            return hops[-proxy_hops]
    return request.client.host if request.client else ""


def visitor_hash(salt: bytes, domain: str, ip: str, user_agent: str) -> str:
    digest = hashlib.sha256(salt + b"|" + f"{domain}|{ip}|{user_agent}".encode())
    return digest.hexdigest()[:32]


async def daily_salt(request: Request, db: AsyncSession) -> bytes:
    today = dt.datetime.now(dt.UTC).date()
    cached = getattr(request.app.state, "salt", None)
    if cached and cached[0] == today:
        return cached[1]
    await db.execute(
        pg_insert(DailySalt).values(day=today, salt=secrets.token_bytes(16)).on_conflict_do_nothing()
    )
    await db.execute(delete(DailySalt).where(DailySalt.day < today))
    salt = await db.scalar(select(DailySalt.salt).where(DailySalt.day == today))
    await db.commit()
    request.app.state.salt = (today, salt)
    return salt


@router.options("/api/e")
async def preflight() -> Response:
    return Response(status_code=204, headers={**CORS, "Access-Control-Allow-Methods": "POST"})


@router.post("/api/e")
async def ingest(request: Request) -> Response:
    try:
        request.app.state.limiter.check("ingest", client_ip(request, request.app.state.settings.proxy_hops), 600, 60)
    except HTTPException as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=CORS) from None
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "event too large", headers=CORS)
    try:
        ev = Event.model_validate_json(body)
    except ValidationError:
        raise HTTPException(400, "not a prodbat event", headers=CORS) from None

    page = urlsplit(ev.u)
    host = normalize_domain(page.hostname or "")
    path = (page.path or "/")[:500]
    user_agent = request.headers.get("user-agent", "")
    accepted = Response(status_code=202, headers=CORS)

    async with request.app.state.sessions() as db:
        project = await db.scalar(select(Project).where(Project.key == ev.k))
        if project is None:
            raise HTTPException(404, "unknown site key", headers=CORS)
        if host != project.domain:
            raise HTTPException(400, f"this key is for {project.domain}", headers=CORS)
        if not user_agent or BOT.search(user_agent) or project.paused_at is not None:
            return accepted
        if project.daily_event_cap:
            try:
                request.app.state.limiter.check("events", str(project.id), project.daily_event_cap, 86400)
            except HTTPException:
                return accepted  # over the day's cap: dropped quietly, the visitor never notices
        if project.first_data_at is None and ev.t == "pv":
            project.first_data_at = dt.datetime.now(dt.UTC)

        ip = client_ip(request, request.app.state.settings.proxy_hops)
        visitor = visitor_hash(await daily_salt(request, db), project.domain, ip, user_agent)

        if ev.t == "pv":
            source, kind = classify(ev.r, ev.u, host)
            db.add(Pageview(project_id=project.id, visitor=visitor, path=path, source=source, source_kind=kind,
                            ref=ev.i))
        elif ev.t == "err":
            if not ev.m:
                raise HTTPException(400, "error without a message", headers=CORS)
            fingerprint = hashlib.sha256(f"{ev.m}|{ev.f}|{ev.l}".encode()).hexdigest()[:16]
            db.add(
                BrowserError(
                    project_id=project.id, visitor=visitor, path=path, fingerprint=fingerprint,
                    message=ev.m, file=ev.f, line=ev.l, col=ev.c, stack=ev.s,
                    browser=noise.browser_name(user_agent),
                    origin=noise.classify(ev.m, ev.f, ev.s, project.domain)[0],
                )
            )
        else:
            # A path has no spaces. Anything else is not a request this script saw.
            if not re.fullmatch(r"/\S*", ev.ru) or not (ev.st == 0 or ev.st >= 500):
                raise HTTPException(400, "not a failed request", headers=CORS)
            db.add(
                FailedRequest(
                    project_id=project.id, visitor=visitor, path=path,
                    method=ev.me.upper(), route=ev.ru.split("?")[0], status=ev.st,
                )
            )
        await db.commit()
    return accepted
