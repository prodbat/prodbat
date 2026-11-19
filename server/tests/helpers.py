import asyncio
import datetime as dt
import html
import os
import re

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from prodbat_server import queue
from prodbat_server.checks import run_due_checks
from prodbat_server.config import Settings
from prodbat_server.detect import evaluate_all
from prodbat_server.models import Email, FailedRequest, Pageview

TEST_DB = os.environ.get("PRODBAT_TEST_DATABASE_URL", "postgresql+psycopg://localhost/prodbat_test")
BROWSER = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15"
SETTINGS = Settings(database_url=TEST_DB, proxy_hops=1, admin_token="admin", public_url="https://pb.example",
                    smtp_host=None, mail_from="alerts@pb.example", watcher_url=None)
NOW = dt.datetime.now(dt.UTC)


class Site:
    """A stand-in for the outside world. `answers` is a list of statuses
    (or "timeout" / "refused") given in turn; the last one repeats."""

    def __init__(self):
        self.answers = [200]
        self.body = '<html><script defer src="https://pb.example/p.js" data-site="pb_test"></script></html>'
        self.headers = {"x-vercel-id": "abc"}
        self.rdap = {"events": [{"eventAction": "expiration", "eventDate": "2030-08-03T00:00:00Z"}]}
        self.osv = {}  # path -> json, for api.osv.dev
        self.json = {}  # path -> json, for endoflife.date, deps.dev and nodejs.org
        # Answers for one address ("http://anysite.com/") or one path ("/.env"), as
        # (status, body, headers). Everything else gets the usual answer.
        self.pages = {}
        self.asked = []  # every address requested of the site itself
        self.lookups = []  # every request to OSV, deps.dev, endoflife.date and nodejs.org

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "rdap.org":
            return httpx.Response(200, json=self.rdap)
        if request.url.host in ("api.osv.dev", "endoflife.date", "api.deps.dev", "nodejs.org"):
            self.lookups.append(f"{request.url.host}{request.url.path}")
        if request.url.host == "api.osv.dev":
            return httpx.Response(200, json=self.osv[request.url.path])
        if request.url.host in ("endoflife.date", "api.deps.dev", "nodejs.org"):
            found = self.json.get(request.url.raw_path.decode().split("?")[0])
            return httpx.Response(200, json=found) if found is not None else httpx.Response(404)
        self.asked.append(str(request.url))
        page = self.pages.get(str(request.url)) or self.pages.get(request.url.path)
        if page is not None:
            status, body, headers = page
            return httpx.Response(status, text=body, headers=headers)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if answer == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if answer == "refused":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(answer, text=self.body, headers=self.headers)


def http_client(site: Site) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(site), follow_redirects=True)


def run(fn, db=None):
    """Run async server code against the test database, the way the worker would."""

    async def go():
        engine = create_async_engine(TEST_DB, poolclass=NullPool)
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    if db is not None:
        db.commit()
    result = asyncio.run(go())
    if db is not None:
        db.expire_all()
    return result


def run_jobs(db, site):
    """The worker's job queue: the daily package checks that are due, then every job that is due."""
    async def go(sessions):
        async with http_client(site) as client:
            await queue.enqueue_due_checks(sessions)
            return await queue.run_due(sessions, client, SETTINGS)

    return run(go, db)


def evaluate(db):
    return run(lambda sessions: evaluate_all(sessions, SETTINGS), db)


def run_checks(db, site):
    async def go(sessions):
        async with http_client(site) as client:
            return await run_due_checks(sessions, client, SETTINGS, recheck_after=0)

    return run(go, db)


def emails(db):
    db.expire_all()
    return db.scalars(select(Email).order_by(Email.id)).all()


def minutes_ago(n):
    return NOW - dt.timedelta(minutes=n)


def days_ago(n):
    return NOW - dt.timedelta(days=n)


def view(project, visitor, path="/", source=None, kind="direct", ts=None, ref=None):
    return Pageview(project_id=project.id, visitor=visitor, path=path, source=source, source_kind=kind,
                    ts=ts or minutes_ago(30), ref=ref)


def failure(project, visitor, ts, route="/api/save", status=500):
    return FailedRequest(project_id=project.id, visitor=visitor, path="/editor", method="POST",
                         route=route, status=status, ts=ts)


def page_text(client, project):
    r = client.get(f"/d/{project.page_token}")
    assert r.status_code == 200
    # Collapse tags and whitespace so assertions read like the page does.
    body = r.text.split("</style>", 1)[1].split("<script>", 1)[0]
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body)))
