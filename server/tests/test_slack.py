import json
import urllib.parse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from prodbat_server.app import create_app
from prodbat_server.detect import evaluate_all
from prodbat_server.models import Incident, SlackPost
from prodbat_server.slack import esc, send_pending_slack

from .helpers import BROWSER, SETTINGS, failure, minutes_ago, page_text, run

SLACKING = SETTINGS.model_copy(update={"slack_client_id": "123.456", "slack_client_secret": "shh"})


class Slack:
    """A stand-in for slack.com: records requests and answers from `answers` by method."""

    def __init__(self):
        self.seen = []
        self.answers = {}
        self.ts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        method = request.url.path.rsplit("/", 1)[-1]
        if method in self.answers:
            return httpx.Response(200, json=self.answers[method])
        if method == "oauth.v2.access":
            return httpx.Response(200, json={
                "ok": True, "access_token": "xoxb-1", "team": {"name": "Acme"},
                "incoming_webhook": {"channel": "#alerts", "channel_id": "C1",
                                     "url": "https://hooks.slack.com/services/T/B/x"}})
        if method == "chat.postMessage":
            self.ts += 1
            return httpx.Response(200, json={"ok": True, "ts": f"100.{self.ts}"})
        return httpx.Response(200, json={"ok": True})

    def posted(self):
        return [json.loads(r.content) for r in self.seen if r.url.path.endswith("chat.postMessage")]


@pytest.fixture
def slack_api():
    return Slack()


@pytest.fixture
def slacking(db, slack_api):
    app = create_app(SLACKING, http=httpx.AsyncClient(transport=httpx.MockTransport(slack_api)))
    with TestClient(app, headers={"user-agent": BROWSER}, follow_redirects=False) as client:
        yield client


def posts(db):
    db.expire_all()
    return db.scalars(select(SlackPost).order_by(SlackPost.id)).all()


def connect(db, project):
    project.slack_token, project.slack_channel_id, project.slack_channel = "xoxb-1", "C1", "#alerts"
    project.slack_webhook_url = "https://hooks.slack.com/services/T/B/x"
    db.commit()


def deliver(db, slack_api):
    async def go(sessions):
        async with httpx.AsyncClient(transport=httpx.MockTransport(slack_api)) as http:
            return await send_pending_slack(sessions, SLACKING, http)
    return run(go, db)


def test_connecting_a_channel(slacking, client, db, project, slack_api):
    assert "Add to Slack" not in page_text(client, project), "not offered when the server has no Slack app"
    assert "Add to Slack" in page_text(slacking, project)

    went = slacking.post(f"/d/{project.page_token}/slack")
    assert went.status_code == 303 and went.headers["location"].startswith("https://slack.com/oauth/v2/authorize?")
    query = dict(urllib.parse.parse_qsl(went.headers["location"].split("?", 1)[1]))
    assert query["scope"] == "incoming-webhook,chat:write"
    assert query["redirect_uri"] == "https://pb.example/slack/callback"

    assert slacking.get("/slack/callback", params={"state": query["state"] + "x", "code": "c"}).status_code == 400
    back = slacking.get("/slack/callback", params={"state": query["state"], "code": "c"})
    assert back.headers["location"] == f"/d/{project.page_token}?sent=slackon#slack"
    db.refresh(project)
    assert (project.slack_token, project.slack_channel_id, project.slack_team) == ("xoxb-1", "C1", "Acme")
    assert project.slack_state is None, "a state works once"
    assert slacking.get("/slack/callback", params={"state": query["state"], "code": "c"}).status_code == 400
    assert "Prodbat will post here" in posts(db)[0].text

    text = page_text(slacking, project)
    assert "Alerts also go to #alerts in Acme" in text
    assert slacking.post(f"/d/{project.page_token}/test-alert").status_code == 303
    assert posts(db)[-1].text.startswith("Test: this is what a Prodbat alert")
    assert "on its way to you@example.com and #alerts in Slack" in \
        page_text(slacking, project) + slacking.get(f"/d/{project.page_token}?sent=test").text

    slacking.post(f"/d/{project.page_token}/slack/remove")
    db.refresh(project)
    assert project.slack_token is None and project.slack_channel_id is None
    assert any(r.url.path.endswith("auth.revoke") for r in slack_api.seen)


def test_cancelling_at_slack_changes_nothing(slacking, db, project):
    went = slacking.post(f"/d/{project.page_token}/slack")
    state = dict(urllib.parse.parse_qsl(went.headers["location"].split("?", 1)[1]))["state"]
    back = slacking.get("/slack/callback", params={"state": state, "error": "access_denied"})
    assert back.headers["location"].endswith("sent=slackno#slack")
    db.refresh(project)
    assert project.slack_token is None


def test_a_problem_is_one_thread(db, project, slack_api):
    connect(db, project)
    db.add_all([failure(project, v, minutes_ago(2)) for v in "abc"])
    run(lambda sessions: evaluate_all(sessions, SLACKING), db)
    assert len(posts(db)) == 1 and posts(db)[0].text == "Broken: POST /api/save is failing on yourapp.com"
    assert deliver(db, slack_api) == 1

    first = slack_api.posted()[0]
    assert first["channel"] == "C1" and "thread_ts" not in first and first["unfurl_links"] is False
    incident = db.scalars(select(Incident)).one()
    assert incident.slack_ts == "100.1"

    run(_recover, db)
    deliver(db, slack_api)
    reply = slack_api.posted()[-1]
    assert reply["thread_ts"] == "100.1" and reply["reply_broadcast"] is True
    assert reply["text"] == "Working again: POST /api/save is failing on yourapp.com"


async def _recover(sessions):
    from prodbat_server.incidents import resolve
    from prodbat_server.models import Project
    async with sessions() as db:
        project = (await db.scalars(select(Project))).one()
        incident = (await db.scalars(select(Incident))).one()
        resolve(db, project, incident, SLACKING, minutes_ago(0), recovery="POST /api/save works again.")
        await db.commit()


def test_strangers_cannot_mention_the_channel(db, project, slack_api):
    connect(db, project)
    assert esc("<!channel> & <https://evil|click>") == "&lt;!channel&gt; &amp; &lt;https://evil|click&gt;"
    run(lambda sessions: _report(sessions, "<!channel> card declined"), db)
    body = posts(db)[0].blocks[0]["blocks"][0]["text"]["text"]
    assert "<!channel>" not in body and "&lt;!channel&gt;" in body


async def _report(sessions, what):
    from prodbat_server.incidents import report
    from prodbat_server.models import Project
    async with sessions() as db:
        project = (await db.scalars(select(Project))).one()
        await report(db, project, SLACKING, kind="test", level="broken", title="Checkout is failing",
                     what=what, who="2 visitors.", started_at=minutes_ago(3))
        await db.commit()


def test_a_removed_app_stops_and_says_so(db, project, slack_api, slacking):
    connect(db, project)
    run(lambda sessions: _report(sessions, "x"), db)
    slack_api.answers["chat.postMessage"] = {"ok": False, "error": "token_revoked"}
    assert deliver(db, slack_api) == 0
    db.refresh(project)
    assert "removed from your Slack" in project.slack_error
    assert posts(db)[0].attempts == 5, "not retried"
    assert "Connect Slack again" in page_text(slacking, project)
    run(lambda sessions: _report(sessions, "y"), db)
    assert len(posts(db)) == 1, "nothing more is queued until it is connected again"


def test_the_webhook_posts_when_the_bot_left_the_channel(db, project, slack_api):
    connect(db, project)
    run(lambda sessions: _report(sessions, "x"), db)
    slack_api.answers["chat.postMessage"] = {"ok": False, "error": "not_in_channel"}
    assert deliver(db, slack_api) == 1
    assert slack_api.seen[-1].url.host == "hooks.slack.com"


def test_a_plan_without_slack_posts_nothing(db, project):
    connect(db, project)
    project.slack_enabled = False
    db.commit()
    run(lambda sessions: _report(sessions, "x"), db)
    assert posts(db) == []
