"""Errors that aren't the site's, the log lines attached to alerts, and the
agent dismissing a task that is not the site's problem."""

import datetime as dt
import json

from sqlalchemy import select

from prodbat_server.models import BrowserError, Incident, LogLine, Mute, ServerError
from prodbat_server.noise import ELSEWHERE, SITE, browser_name, classify
from prodbat_server.summary import evidence_for

from .helpers import NOW, failure, minutes_ago, run, evaluate, page_text, view
from .test_api import rpc

SAFARI = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/18.1 Mobile/15E148 Safari/604.1")
INSTAGRAM = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Mobile/22B83 Instagram 356.0.0.24.104 (iPhone15,2; iOS 18_1)")
CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
# What the youfiliate agent found: Safari's wording, located in the page's own HTML, in no file the site serves.
INJECTED = {"m": "TypeError: undefined is not an object (evaluating 'a.standardSelectors')",
            "f": "https://yourapp.com/register", "l": 17, "c": 6302,
            "s": "@https://yourapp.com/register:17:6302\nforEach@[native code]"}
OURS = {"m": "TypeError: cart is undefined", "f": "https://yourapp.com/assets/index-4f2a.js", "l": 1, "c": 9120,
        "s": "pay@https://yourapp.com/assets/index-4f2a.js:1:9120"}


def send(client, error, ip, ua, page="https://yourapp.com/register"):
    body = {"k": "pb_test", "t": "err", "u": page, **error}
    r = client.post("/api/e", content=json.dumps(body), headers={"x-forwarded-for": ip, "user-agent": ua})
    assert r.status_code == 202, r.text


def test_whose_error_it_is():
    assert classify(OURS["m"], OURS["f"], OURS["s"], "yourapp.com") == (SITE, "")
    # A script on a subdomain the site serves counts as its own.
    assert classify("x", "https://static.yourapp.com/app.mjs?v=3", "", "yourapp.com")[0] == SITE
    origin, why = classify(INJECTED["m"], INJECTED["f"], INJECTED["s"], "yourapp.com")
    assert origin == ELSEWHERE and "inside the page" in why
    assert classify("x", "", "f@safari-web-extension://AB12/content.js:3:1", "yourapp.com")[0] == ELSEWHERE
    assert classify("x", "webkit-masked-url://hidden/", "", "yourapp.com")[0] == ELSEWHERE
    origin, why = classify("x", "https://www.googletagmanager.com/gtag/js?id=G-1", "", "yourapp.com")
    assert origin == ELSEWHERE and "www.googletagmanager.com" in why
    assert classify("Script error.", "", "", "yourapp.com")[0] == ELSEWHERE
    # No location at all could be anyone's, so it stays the site's: an unhandled rejection, say.
    assert classify("Request failed", "", "", "yourapp.com")[0] == SITE
    # A trace that passes through the site's own code is the site's, wherever it started.
    assert classify("x", "https://cdn.other.com/lib.js", "at a (https://cdn.other.com/lib.js:1:1)\n"
                    "at b (https://yourapp.com/_next/static/chunks/page-1.js:2:3)", "yourapp.com")[0] == SITE


def test_browser_names():
    assert browser_name(SAFARI) == "Safari 18"
    assert browser_name(INSTAGRAM) == "Instagram's built-in browser"
    assert browser_name(CHROME) == "Chrome 131"
    assert browser_name("Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0") == "Firefox 133"
    assert browser_name("Mozilla/5.0 ... Chrome/130.0.0.0 Safari/537.36 Edg/130.0.2849.68") == "Edge 130"


def test_an_injected_script_error_is_kept_but_is_not_an_alert(client, db, project):
    for ip in ("203.0.113.1", "203.0.113.2", "203.0.113.3"):
        send(client, INJECTED, ip, SAFARI)
    send(client, OURS, "203.0.113.4", CHROME, page="https://yourapp.com/checkout")
    send(client, OURS, "203.0.113.5", INSTAGRAM, page="https://yourapp.com/checkout")
    stored = db.scalars(select(BrowserError).order_by(BrowserError.id)).all()
    assert [(e.origin, e.browser) for e in stored] == [("elsewhere", "Safari 18")] * 3 + [
        ("site", "Chrome 131"), ("site", "Instagram's built-in browser")]

    evaluate(db)
    [incident] = db.scalars(select(Incident)).all()
    assert incident.kind == "error" and incident.title == "An error in your code on /checkout"

    # Both are still on the Errors tab; the injected one is labelled.
    errors = client.get(f"/d/{project.page_token}/errors").text
    assert "standardSelectors" in errors and errors.count("Probably not your code</span>") == 1
    injected = next(e.fingerprint for e in stored if e.origin == "elsewhere")
    detail = client.get(f"/d/{project.page_token}/errors/browser/{injected}").text
    assert "Probably not your code" in detail and "isn&#39;t in any of the site&#39;s files" in detail
    assert "Safari 18" in detail


def test_a_browser_error_says_which_browsers_and_whether_visitors_went_on(client, db, project):
    send(client, OURS, "203.0.113.4", CHROME, page="https://yourapp.com/checkout")
    send(client, OURS, "203.0.113.5", SAFARI, page="https://yourapp.com/checkout")
    stored = db.scalars(select(BrowserError)).all()
    # One went on to the thank-you page, one saw nothing after it.
    went_on = next(e for e in stored if e.browser == "Chrome 131")
    db.add(view(project, went_on.visitor, path="/thanks", ts=went_on.ts + dt.timedelta(seconds=20)))
    db.add(view(project, went_on.visitor, path="/checkout", ts=went_on.ts - dt.timedelta(seconds=20)))
    db.add(LogLine(project_id=project.id, level="error", message="unrelated server warning", ts=went_on.ts))
    db.commit()
    evaluate(db)
    incident = db.scalars(select(Incident)).one()
    facts = run(lambda sessions: _evidence(sessions, project.id, incident.id), db)
    assert "Browsers: Chrome 131 (1), Safari 18 (1)." in facts
    assert "1 of 2 visitors went on to other pages afterwards (/thanks); 1 saw no other page after it." in facts
    assert not any("Logged around" in f for f in facts)  # server logs say nothing about a browser error


async def _evidence(sessions, project_id, incident_id):
    from prodbat_server.models import Project

    async with sessions() as db:
        return await evidence_for(db, await db.get(Project, project_id), await db.get(Incident, incident_id))


def test_logs_attached_to_an_alert_are_the_related_ones(client, db, project):
    started = minutes_ago(30)
    db.add(Incident(project_id=project.id, kind="server", key="fp9", level="look", title="POST /api/save failed",
                    what="", who="", started_at=started,
                    data={"route": "POST /api/save", "kind": "KeyError", "where": "app.py:3"}))
    # "Not Found: /" is logged every hour of the day, so it is routine.
    for hour in range(1, 13):
        db.add(LogLine(project_id=project.id, level="warning", message="Not Found: /", ts=started - dt.timedelta(hours=hour)))
    db.add_all([LogLine(project_id=project.id, level="warning", message="Not Found: /", ts=started + dt.timedelta(seconds=s))
                for s in (5, 50)])
    db.add(LogLine(project_id=project.id, level="warning", message="cache miss for user 81", ts=started - dt.timedelta(minutes=2)))
    db.add_all([LogLine(project_id=project.id, level="error", message=f"KeyError 'price_id' in /api/save for order {n}",
                        ts=started + dt.timedelta(seconds=n)) for n in (10, 20, 30)])
    db.add(LogLine(project_id=project.id, level="info", message="user signed up", ts=started))
    db.commit()
    incident = db.scalars(select(Incident)).one()
    facts = run(lambda sessions: _evidence(sessions, project.id, incident.id), db)
    logged = facts[facts.index("Logged around that time:") + 1:]
    assert "KeyError 'price_id' in /api/save for order 10  (and 2 more like it)" in logged[0]  # named the route: first
    assert "cache miss for user 81" in logged[1]
    assert not any("Not Found" in line for line in logged[:-1])
    assert logged[-1] == "(1 kind of line left out: it is logged all day, not only around this.)"
    assert not any("signed up" in line for line in logged)


def two_visitors_hit(db, project, fingerprint="fp1", visitors="ab", ts=None):
    db.add_all([BrowserError(project_id=project.id, visitor=v, path="/onboarding", fingerprint=fingerprint,
                             message="TypeError: x is undefined", file="https://yourapp.com/app.js", line=1, col=1,
                             stack="", ts=ts or minutes_ago(5)) for v in visitors])
    db.commit()


def test_the_agent_dismisses_a_task_that_is_not_the_sites_problem(client, db, project, key):
    two_visitors_hit(db, project)
    evaluate(db)
    incident = db.scalars(select(Incident)).one()
    tid = f"T{incident.id}"
    text = client.get(f"/api/tasks/{tid}", headers=key).json()["text"]
    assert f"npx prodbat dismiss {tid} --reason" in text

    short = client.post(f"/api/tasks/{tid}/dismiss", headers=key, json={"reason": "noise"})
    assert short.status_code == 400 and "Say why in a sentence" in short.json()["detail"]
    reason = "It comes from a Safari extension, not the site's code."
    done = client.post(f"/api/tasks/{tid}/dismiss", headers=key, json={"reason": reason}).json()
    assert done["dismissed"] is True and "unless it reaches 10 visitors in a day" in done["text"]
    db.expire_all()
    assert db.get(Incident, incident.id).ended_at is not None
    mute = db.scalars(select(Mute)).one()
    assert (mute.by, mute.reason, mute.reach) == ("agent", reason, 2)

    # The owner sees what the agent decided, and why.
    db.add(view(project, "a"))
    db.commit()
    assert f"Your coding agent dismissed “An error in your code on /onboarding”: {reason}" in page_text(client, project)

    # More visitors, but not many more: still dismissed.
    two_visitors_hit(db, project, visitors="cdefg", ts=dt.datetime.now(dt.UTC))
    evaluate(db)
    assert db.scalars(select(Incident).where(Incident.ended_at.is_(None))).all() == []

    # It grows: it comes back, and says it was dismissed before.
    two_visitors_hit(db, project, visitors="hijklmnop", ts=dt.datetime.now(dt.UTC))
    evaluate(db)
    back = db.scalars(select(Incident).where(Incident.ended_at.is_(None))).one()
    assert back.data["dismissed"] == reason
    assert db.scalars(select(Mute)).all() == []
    text = client.get(f"/api/tasks/T{back.id}", headers=key).json()["text"]
    assert f"It was dismissed before as not the site's problem (“{reason}”), and has grown since" in text


def test_what_cannot_be_dismissed(client, db, project, key):
    db.add(Incident(project_id=project.id, kind="advisory", key="npm/x", level="look", started_at=NOW, title="t",
                    what="w", who="w", data={"ecosystem": "npm", "name": "x", "version": "1.0.0", "to": "1.0.1"}))
    db.add_all([failure(project, v, minutes_ago(2), route="/api/checkout") for v in "abc"])
    db.commit()
    evaluate(db)
    reason = {"reason": "I do not think this one matters at all."}
    for incident in db.scalars(select(Incident)).all():
        r = client.post(f"/api/tasks/T{incident.id}/dismiss", headers=key, json=reason)
        assert r.status_code == 400
        assert ("cannot be dismissed" in r.json()["detail"])
    assert client.post("/api/tasks/T999999/dismiss", headers=key, json=reason).status_code == 404
    assert db.scalars(select(Mute)).all() == []


def test_a_server_error_can_be_dismissed_over_mcp(client, db, project, key):
    db.add(ServerError(project_id=project.id, fingerprint="fp2", kind="NotFound", message="Not Found: /xmlrpc.php",
                       route="GET /xmlrpc.php", where="", trace="", status=500, ts=minutes_ago(3)))
    db.commit()
    evaluate(db)
    incident = db.scalars(select(Incident)).one()
    said = rpc(client, "tools/call", {"name": "prodbat_dismiss_task", "arguments": {
        "id": f"T{incident.id}", "reason": "A bot probing for xmlrpc.php, which this app does not have."}}, key)
    assert "Dismissed" in said["result"]["content"][0]["text"] and "10 errors" in said["result"]["content"][0]["text"]
    assert "prodbat_dismiss_task" in rpc(client, "initialize", {}, key)["result"]["instructions"]
