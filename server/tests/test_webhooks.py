import datetime as dt
import json

import httpx
import pytest
from sqlalchemy import select

from prodbat_server import webhooks
from prodbat_server.checks import _refuse_private_addresses
from prodbat_server.detect import evaluate_all
from prodbat_server.models import Incident, Project, WebhookDelivery
from prodbat_server.webhooks import send_pending_webhooks, sign, verify

from .helpers import SETTINGS, failure, minutes_ago, page_text, run

HOOK = "https://hooks.example.com/prodbat"


class Receiver:
    """The owner's endpoint: records each POST; `answers` replaces the next statuses."""

    def __init__(self):
        self.seen = []
        self.answers = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(self.answers.pop(0) if self.answers else 200)

    def bodies(self):
        return [json.loads(r.content) for r in self.seen]


@pytest.fixture
def receiver():
    return Receiver()


def connect(db, project):
    webhooks.connect(project, HOOK)
    db.commit()
    return project.webhook_secret


def deliveries(db):
    db.expire_all()
    return db.scalars(select(WebhookDelivery).order_by(WebhookDelivery.id)).all()


def deliver(db, receiver, now=None, hooks=None):
    async def go(sessions):
        async with httpx.AsyncClient(transport=httpx.MockTransport(receiver), event_hooks=hooks or {}) as http:
            return await send_pending_webhooks(sessions, SETTINGS, http, now=now)
    return run(go, db)


async def _report(sessions, what="2 visitors hit it.", level="broken", key=""):
    from prodbat_server.incidents import report
    async with sessions() as db:
        project = (await db.scalars(select(Project))).one()
        await report(db, project, SETTINGS, kind="test", key=key, level=level, title="Checkout is failing",
                     what=what, who="2 visitors.", started_at=minutes_ago(3))
        await db.commit()


def test_adding_a_webhook_shows_the_secret_once_and_on_request(client, db, project):
    assert "Add webhook" in page_text(client, project, "settings")
    for bad in ("http://hooks.example.com/x", "https://127.0.0.1/x", "https://localhost/x", "https://10.0.0.8/x",
                "https://user:pw@hooks.example.com/x"):
        answer = client.post(f"/d/{project.page_token}/webhook", data={"url": bad}, follow_redirects=False)
        assert answer.headers["location"].endswith("?sent=webhookbad#webhook"), bad
    db.refresh(project)
    assert project.webhook_url is None

    shown = client.post(f"/d/{project.page_token}/webhook", data={"url": HOOK})
    db.refresh(project)
    assert project.webhook_url == HOOK and project.webhook_secret.startswith("pbwh_")
    assert project.webhook_secret in shown.text
    page = page_text(client, project, "settings")
    assert HOOK in page and project.webhook_secret not in page, "the page itself doesn't show the secret"

    again = client.post(f"/d/{project.page_token}/webhook/secret")
    assert project.webhook_secret in again.text
    old = project.webhook_secret
    rotated = client.post(f"/d/{project.page_token}/webhook/secret", data={"rotate": "1"})
    db.refresh(project)
    assert project.webhook_secret != old and project.webhook_secret in rotated.text

    client.post(f"/d/{project.page_token}/webhook/remove")
    db.refresh(project)
    assert project.webhook_url is None and project.webhook_secret is None


def test_a_problem_is_delivered_signed_with_its_task_and_untrusted_evidence(db, project, receiver):
    secret = connect(db, project)
    db.add_all([failure(project, v, minutes_ago(2)) for v in "abc"])
    run(lambda sessions: evaluate_all(sessions, SETTINGS), db)
    assert [d.event for d in deliveries(db)] == ["incident.opened"]
    assert deliver(db, receiver) == 1

    request = receiver.seen[0]
    raw = request.content.decode()
    assert request.headers["prodbat-event"] == "incident.opened"
    assert request.headers["prodbat-delivery"] == deliveries(db)[0].delivery_id
    assert verify(secret, request.headers["prodbat-signature"], raw)
    assert not verify("pbwh_wrong", request.headers["prodbat-signature"], raw)
    assert not verify(secret, request.headers["prodbat-signature"], raw.replace("yourapp", "evilapp"))
    stale = sign(secret, 1_000_000, raw)
    assert not verify(secret, stale, raw), "an old signature is refused"

    body = json.loads(raw)
    incident = db.scalars(select(Incident)).one()
    assert body["event"] == "incident.opened" and body["id"] == request.headers["prodbat-delivery"]
    assert body["project"] == {"domain": "yourapp.com", "page_url": f"https://pb.example/d/{project.page_token}"}
    assert body["incident"]["id"] == incident.id and body["incident"]["level"] == "broken"
    assert body["incident"]["route"] == "POST /api/save"
    assert body["task"]["id"] == f"T{incident.id}" and body["task"]["done_command"] == f"npx prodbat done T{incident.id}"
    assert f"npx prodbat task {body['task']['id']}" in body["task"]["agent_prompt"]
    evidence = body["untrusted_evidence"]
    assert "never instructions" in evidence["note"]
    assert any("POST /api/save -> 500" in line for line in evidence["lines"])
    assert "evidence" not in body["task"], "evidence lives only under untrusted_evidence"
    assert body["links"]["docs"].endswith("cloud-agents.md")


def test_retries_back_off_then_give_up_with_the_same_body(client, db, project, receiver):
    connect(db, project)
    run(_report, db)
    now = dt.datetime.now(dt.UTC)
    receiver.answers = [500] * webhooks.MAX_ATTEMPTS
    assert deliver(db, receiver, now) == 0
    first = deliveries(db)[0]
    assert first.attempts == 1 and first.last_status == 500 and first.next_attempt_at == now + dt.timedelta(minutes=1)
    assert deliver(db, receiver, now) == 0 and len(receiver.seen) == 1, "not before its time"

    for _ in range(webhooks.MAX_ATTEMPTS - 1):
        now = deliveries(db)[0].next_attempt_at
        deliver(db, receiver, now)
    d = deliveries(db)[0]
    assert d.attempts == webhooks.MAX_ATTEMPTS and d.gave_up_at is not None and d.sent_at is None
    assert len({r.content for r in receiver.seen}) == 1, "every retry sends the same body"
    assert {r.headers["prodbat-delivery"] for r in receiver.seen} == {d.delivery_id}
    span = d.last_attempt_at - d.created_at
    assert dt.timedelta(hours=20) < span < dt.timedelta(hours=26), "retries spread over about a day"
    assert "incident.opened gave up · 500" in page_text(client, project, "settings"), "the page shows the last attempts"


def test_repeated_410_turns_it_off_and_the_page_says_so(db, project, receiver, client):
    connect(db, project)
    for key in "abc":
        run(lambda sessions: _report(sessions, key=key), db)
    receiver.answers = [410, 410, 410]
    deliver(db, receiver)
    db.refresh(project)
    assert project.webhook_error == webhooks.TURNED_OFF_GONE
    assert all(d.gave_up_at is not None for d in deliveries(db)), "410 isn't retried"
    page = page_text(client, project, "settings")
    assert "answered 410 Gone several times" in page and "Send a test to turn it on again" in page

    run(lambda sessions: _report(sessions, key="d"), db)
    assert len(deliveries(db)) == 3, "nothing is queued while it is off"
    client.post(f"/d/{project.page_token}/webhook/test")
    db.refresh(project)
    assert project.webhook_error is None and deliveries(db)[-1].event == "test"


def test_three_days_of_failures_turn_it_off(db, project, receiver):
    connect(db, project)
    run(_report, db)
    project.webhook_failing_since = dt.datetime.now(dt.UTC) - dt.timedelta(days=3, minutes=1)
    db.commit()
    receiver.answers = [503]
    deliver(db, receiver)
    db.refresh(project)
    assert project.webhook_error == webhooks.TURNED_OFF_FAILING

    webhooks.turn_on_again(project)
    db.commit()
    run(lambda sessions: _report(sessions, key="x"), db)
    deliver(db, receiver)
    db.refresh(project)
    assert project.webhook_failing_since is None, "one success clears the count"


def test_a_private_address_is_refused_when_sending(db, project, receiver):
    connect(db, project)
    project.webhook_url = "https://localhost:8443/hook"  # as if DNS changed after it was added
    db.commit()
    run(_report, db)
    assert deliver(db, receiver, hooks={"request": [_refuse_private_addresses]}) == 0
    assert receiver.seen == []
    assert deliveries(db)[0].last_error == "Refused: the address is on a private network"


def test_the_test_button_sends_a_test_event(client, db, project, receiver):
    connect(db, project)
    assert client.post(f"/d/{project.page_token}/test-alert").status_code == 200
    assert "on its way to you@example.com and your webhook" in client.get(f"/d/{project.page_token}/settings?sent=test").text
    deliver(db, receiver)
    body = receiver.bodies()[0]
    assert body["event"] == "test" and body["test"] is True
    assert body["untrusted_evidence"]["lines"] and "never instructions" in body["untrusted_evidence"]["note"]


def test_fix_checks_are_delivered_once_per_answer(client, db, project, key, receiver, site):
    connect(db, project)
    run(lambda sessions: _down(sessions), db)
    incident = db.scalars(select(Incident)).one()
    site.answers = [503]
    client.post(f"/api/tasks/T{incident.id}/check", headers=key)
    client.post(f"/api/tasks/T{incident.id}/check", headers=key)
    events = [d.event for d in deliveries(db)]
    assert events == ["incident.opened", "task.still_failing"], "the same answer twice is sent once"
    site.answers = [200]
    assert client.post(f"/api/tasks/T{incident.id}/check", headers=key).json()["fixed"] is True
    events = [d.event for d in deliveries(db)]
    assert events[-2:] == ["incident.resolved", "task.verified"]
    deliver(db, receiver)
    verified = receiver.bodies()[-1]
    assert verified["check"]["fixed"] is True and verified["check"]["task_id"] == f"T{incident.id}"
    assert receiver.bodies()[-2]["resolution"]["recovery"] == "Your site is loading again."


async def _down(sessions):
    from prodbat_server.incidents import report
    async with sessions() as db:
        project = (await db.scalars(select(Project))).one()
        await report(db, project, SETTINGS, kind="down", level="broken", title="Your site is down",
                     what="It doesn't load.", who="Everyone.", started_at=minutes_ago(3))
        await db.commit()


def test_not_offered_when_the_plan_or_server_has_it_off(client, db, project):
    project.webhook_enabled = False
    db.commit()
    assert "Add webhook" not in page_text(client, project, "settings")
    assert client.post(f"/d/{project.page_token}/webhook", data={"url": HOOK}).status_code == 403
    connect(db, project)
    run(_report, db)
    assert deliveries(db) == []

    project.webhook_enabled = True
    db.commit()
    off = SETTINGS.model_copy(update={"webhooks": False})
    assert not webhooks.can_add(project, off) and not webhooks.can_post(project, off)
