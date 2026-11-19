import datetime as dt

import pytest
from sqlalchemy import select

from prodbat_server.flows import discover_pages, flow_for
from prodbat_server.freshness import find_cycle, supported_cycle
from prodbat_server.models import Dependency, Incident, LogLine, QueueReport, ServerError, SiteCheck
from prodbat_server.worker import prune

from .helpers import NOW, SETTINGS, days_ago, emails, evaluate, failure, http_client, minutes_ago, page_text, run, run_checks, run_jobs, view


def incidents(db):
    db.expire_all()
    return db.scalars(select(Incident).order_by(Incident.id)).all()


# ---- paying, signing in, signing up --------------------------------------

@pytest.mark.parametrize("route, flow", [
    ("POST /api/checkout", "pay"), ("POST /api/stripe/webhook", "pay"), ("/billing/portal", "pay"),
    ("POST /api/create-checkout-session", "pay"), ("GET /api/subscriptions/42", "pay"),
    ("POST /api/auth/login", "signin"), ("POST /login", "signin"), ("/api/session", "signin"), ("/sign-in", "signin"),
    ("POST /api/auth/signup", "signup"), ("/register", "signup"),
    ("GET /api/posts", None), ("/api/paystubs", None), ("/authors", None), ("/api/reorder-list", None), ("/display", None),
])
def test_routes_are_recognised_by_name(route, flow):
    assert flow_for(None, route) == flow


def test_the_owner_can_name_a_route_that_is_not_recognised(client, db, project, key):
    assert flow_for(project, "POST /api/buy") is None
    r = client.put("/api/config", json={"flows": {"pay": ["/api/buy"]}, "pages": ["/login"]}, headers=key)
    assert r.json() == {"ok": True, "flows": {"pay": ["/api/buy"]}, "pages": ["/login"]}
    db.expire_all()
    assert flow_for(project, "POST /api/buy") == "pay" and flow_for(project, "POST /api/buy/confirm") == "pay"
    assert client.put("/api/config", json={"flows": {"dance": ["/x"]}}, headers=key).status_code == 400
    assert client.put("/api/config", json={"pages": ["login"]}, headers=key).status_code == 400


def test_one_failed_checkout_is_enough_and_the_alert_says_what_people_cannot_do(client, db, project):
    db.add(failure(project, "a", minutes_ago(2), route="/api/checkout"))
    evaluate(db)
    incident, = incidents(db)
    assert (incident.level, incident.title) == ("broken", "People cannot pay")
    assert incident.what == ("POST /api/checkout answered with an error (500) for 1 visitor in the last 15 minutes. "
                             "This is part of your checkout.")
    assert emails(db)[0].subject == "Broken: People cannot pay on yourapp.com"
    assert "People cannot pay." in page_text(client, project)


def test_a_server_error_while_signing_in_is_broken_at_once_and_an_ordinary_one_is_not(client, db, project, key):
    client.post("/api/errors", headers=key, json={"kind": "OperationalError", "message": "too many connections",
                "route": "POST /api/auth/login", "where": "auth/views.py:31 in login", "trace": "Traceback"})
    client.post("/api/errors", headers=key, json={"kind": "HTTP 502", "status": 502, "route": "GET /api/posts"})
    evaluate(db)
    signin, other = sorted(incidents(db), key=lambda i: i.level)
    assert (signin.level, signin.title) == ("broken", "People cannot sign in")
    assert "Cause: OperationalError: too many connections at auth/views.py:31 in login. This is part of your sign-in." in signin.what
    assert (other.level, other.title, other.what) == (
        "look", "GET /api/posts failed", "GET /api/posts failed 1 time in the last 24 hours. Cause: HTTP 502.")
    assert db.scalars(select(ServerError).where(ServerError.status == 502)).one().kind == "HTTP 502"

    from prodbat_server.models import Incident as I  # the past tense, once it is over
    from prodbat_server.summary import past_tense
    done = I(kind="server", key="k", level="broken", title="People cannot sign in", what="", who="",
             started_at=minutes_ago(30), ended_at=minutes_ago(10))
    assert past_tense(done) == "People could not sign in for 20 minutes."


# ---- other pages from outside ---------------------------------------------

def test_sign_in_and_pricing_pages_linked_from_the_home_page_are_found():
    html = """<a href="/login">Log in</a> <a href="https://www.yourapp.com/pricing/">Pricing</a> <a href="/signup?ref=x">Join</a>
              <a href="/blog/how-to-login">post</a> <a href="https://other.com/login">x</a> <a href="/auth.css">x</a> <a href="/about">x</a>"""
    assert discover_pages("yourapp.com", html) == ["/login", "/pricing", "/signup"]


def test_verify_starts_watching_those_pages_and_a_broken_sign_in_page_is_reported(client, db, project, key, site):
    site.body += '<a href="/login">Log in</a>'
    text = client.post("/api/verify", headers=key).json()["text"]
    assert "ok   Other pages are watched. Also loaded from outside every 5 minutes: /login." in text
    db.expire_all()
    assert project.pages == ["/login"]

    site.answers = [200, 500]  # the home page loads, the sign-in page does not
    run_checks(db, site)
    incident, = incidents(db)
    assert (incident.kind, incident.key, incident.title) == ("page", "/login", "Your sign-in page is not loading")
    assert incident.what == "yourapp.com/login answered with an error (500). The home page loads. We tried twice from outside."
    assert {c.path for c in db.scalars(select(SiteCheck))} == {"/", "/login"}

    project.next_check_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    site.answers = [200]
    run_checks(db, site)
    assert incidents(db)[0].ended_at is not None
    assert "The page is loading again: we loaded it from outside in" in emails(db)[-1].body


# ---- job queues --------------------------------------------------------------

def queue(project, minutes, **fields):
    return QueueReport(project_id=project.id, system="bullmq", name="emails", ts=minutes_ago(minutes), **fields)


def test_a_queue_is_reported_and_shown_with_what_is_waiting(client, db, project, key):
    r = client.post("/api/queues", headers=key, json={"system": "bullmq", "queues": [
        {"name": "emails", "waiting": 3, "oldest_seconds": 12.4, "done": 40, "failed": 1}]})
    assert r.status_code == 202
    assert client.post("/api/queues", headers=key, json={"queues": [{"name": "bad name!"}]}).status_code == 400
    db.add(view(project, "a"))
    evaluate(db)
    assert incidents(db) == []
    text = page_text(client, project)
    assert "Background jobs" in text and "emails queue 3 jobs waiting, the oldest for 1 minute, as of" in text


def test_jobs_waiting_too_long_are_stuck_and_then_moving_again(client, db, project, key):
    db.add(queue(project, 1, waiting=140, oldest_seconds=1320, done=0))
    evaluate(db)
    incident, = incidents(db)
    assert (incident.kind, incident.level, incident.title) == ("queue", "broken", "Jobs in your emails queue are stuck")
    assert incident.what == "140 jobs waiting, and the oldest has waited 22 minutes."
    status = client.get("/api/status", headers=key).json()
    assert "bullmq queue emails: waiting=140 oldest_seconds=1320" in status["problems"][0]["evidence"][0]

    db.add(QueueReport(project_id=project.id, system="bullmq", name="emails", waiting=0, oldest_seconds=0, done=140))
    evaluate(db)
    assert incidents(db)[0].ended_at is not None
    assert "The queue is moving again." in emails(db)[-1].body


def test_a_queue_that_only_reports_latency_is_judged_on_that(db, project):
    # Celery through signals cannot see the queue itself, only how long jobs waited.
    db.add(QueueReport(project_id=project.id, system="celery", name="celery", ts=minutes_ago(1), max_wait_seconds=2000, done=3))
    evaluate(db)
    assert incidents(db)[0].what == "Jobs are waiting, and the oldest has waited 33 minutes."


def test_jobs_waiting_with_none_finishing_are_stuck_even_when_the_wait_is_unknown(db, project):
    # The web process still reports what is waiting; the workers that would report the rest are gone.
    db.add_all([QueueReport(project_id=project.id, system="celery", name="celery", ts=minutes_ago(m), waiting=w)
                for m, w in [(13, 4), (9, 9), (5, 15), (1, 22)]])
    evaluate(db)
    incident, = incidents(db)
    assert incident.title == "Jobs in your celery queue are stuck"
    assert incident.what == "22 jobs waiting, and none has finished in the last 15 minutes. The workers may have stopped."


def test_a_queue_that_stops_reporting_with_jobs_waiting_has_gone_quiet(db, project):
    db.add_all([queue(project, m, waiting=5, oldest_seconds=30, done=10) for m in range(40, 140, 10)])
    evaluate(db)
    incident, = incidents(db)
    assert (incident.level, incident.title) == ("broken", "Your emails queue has gone quiet")
    assert "stopped 40 minutes ago, with 5 jobs waiting. The workers may have stopped." in incident.what


def test_many_failing_jobs_need_a_look_but_a_few_do_not(db, project):
    db.add_all([queue(project, m, waiting=0, oldest_seconds=0, done=3, failed=2) for m in (30, 20, 10)])
    evaluate(db)
    incident, = incidents(db)
    assert (incident.level, incident.title, incident.what) == (
        "look", "Jobs in your emails queue are failing", "6 jobs failed in the last hour, out of 15.")


def test_a_healthy_busy_queue_is_not_a_problem(db, project):
    db.add_all([queue(project, m, waiting=20, oldest_seconds=45, done=500, failed=2) for m in (30, 20, 10, 1)])
    evaluate(db)
    assert incidents(db) == []


# ---- logs ----------------------------------------------------------------------

def test_log_lines_are_stored_found_and_shown(client, db, project, key):
    r = client.post("/api/logs", headers=key, json={"lines": [
        {"message": "user signed up", "level": "info", "fields": {"plan": "pro"}},
        {"message": "Stripe webhook retry", "level": "WARN", "t": NOW.timestamp() - 600},
        {"message": "could not send welcome email", "level": "fatal", "fields": {"to": "a@b.co"}},
        {"message": "x" * 9000, "level": "nonsense", "t": 12345.0},
    ]})
    assert r.json() == {"ok": True, "stored": 4, "dropped": 0}
    lines = db.scalars(select(LogLine).order_by(LogLine.id)).all()
    assert [line.level for line in lines] == ["info", "warning", "error", "info"]
    assert len(lines[3].message) == 4000 and lines[3].ts > NOW - dt.timedelta(minutes=5)  # a wild clock is ignored

    found = client.get("/api/logs", headers=key, params={"level": "warning"}).json()
    assert [line["message"] for line in found["lines"]] == ["Stripe webhook retry", "could not send welcome email"]
    assert 'ERROR   could not send welcome email {"to":"a@b.co"}' in found["text"]
    assert [line["message"] for line in client.get("/api/logs", headers=key, params={"q": "SIGNED"}).json()["lines"]] == ["user signed up"]
    assert client.get("/api/logs", headers=key, params={"since": "5m", "q": "webhook"}).json()["lines"] == []
    assert client.get("/api/logs", headers=key, params={"since": "ages"}).status_code == 400

    html = client.get(f"/d/{project.page_token}/logs", params={"q": "welcome"}).text
    assert "could not send welcome email" in html and "user signed up" not in html
    assert "kept for 7 days" in html
    db.add(view(project, "a"))
    db.commit()
    assert "Logs" in page_text(client, project)


def test_a_flood_of_log_lines_is_cut_off_not_refused(client, project, key):
    lines = [{"message": f"line {i}"} for i in range(500)]
    stored = [client.post("/api/logs", headers=key, json={"lines": lines}).json()["stored"] for _ in range(8)]
    assert sum(stored) == SETTINGS.log_lines_per_minute and stored[-1] == 0


def test_what_the_app_logged_around_a_problem_is_given_to_the_agent(client, db, project, key):
    db.add(failure(project, "a", minutes_ago(3), route="/api/checkout"))
    db.add_all([
        LogLine(project_id=project.id, ts=minutes_ago(4), level="error", message="price_id missing from cart"),
        LogLine(project_id=project.id, ts=minutes_ago(4), level="info", message="request started"),
        LogLine(project_id=project.id, ts=minutes_ago(300), level="error", message="unrelated, hours earlier"),
    ])
    evaluate(db)
    evidence = client.get("/api/status", headers=key).json()["problems"][0]["evidence"]
    assert "Logged around that time:" in evidence
    assert any("price_id missing from cart" in line for line in evidence)
    assert not any("request started" in line or "unrelated" in line for line in evidence)


def test_logs_are_kept_for_the_servers_number_of_days_or_the_projects_own(db, project):
    db.add_all([LogLine(project_id=project.id, ts=days_ago(d), level="info", message=f"{d} days old") for d in (1, 10, 40)])
    run(lambda sessions: prune(sessions, SETTINGS), db)
    assert [line.message for line in db.scalars(select(LogLine).order_by(LogLine.id))] == ["1 days old"]

    project.log_keep_days = 30  # a paid plan, say
    db.add_all([LogLine(project_id=project.id, ts=days_ago(d), level="info", message=f"{d} days old") for d in (10, 40)])
    run(lambda sessions: prune(sessions, SETTINGS), db)
    assert [line.message for line in db.scalars(select(LogLine).order_by(LogLine.id))] == ["1 days old", "10 days old"]


# ---- end of life and current versions -------------------------------------------

NODE = [{"cycle": "26", "eol": "2029-04-30", "lts": "2026-10-28"}, {"cycle": "25", "eol": "2026-06-01", "lts": False},
        {"cycle": "24", "eol": "2028-04-30", "lts": "2025-10-28"}, {"cycle": "20", "eol": "2026-04-30", "lts": "2023-10-24"}]


def test_release_lines_and_what_to_move_to():
    today = dt.date(2026, 11, 18)
    assert find_cycle(NODE, "20.11.1")["cycle"] == "20"
    assert find_cycle([{"cycle": "3.12"}, {"cycle": "3"}], "3.12.1")["cycle"] == "3.12"
    assert find_cycle(NODE, "17.0.0") is None
    assert supported_cycle(NODE, today) == "26"
    assert supported_cycle(NODE, dt.date(2026, 9, 1)) == "24"  # 26 was not long-term-support yet
    assert supported_cycle([{"cycle": "5", "eol": True}, {"cycle": "6", "eol": False}], today) == "6"


def versions(site, db, node_releases=()):
    run_jobs(db, site)


def dev(name, version, deprecated="", system="NPM"):
    """One answer in a deps.dev batch."""
    key = {"system": system, "name": name, "version": version}
    return {"request": {"versionKey": key},
            "version": {"versionKey": key, "isDeprecated": bool(deprecated), "deprecatedReason": deprecated}}


def test_an_unsupported_language_or_framework_needs_a_look(client, db, project, key, site):
    soon = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    site.json = {"/api/nodejs.json": NODE,
                 "/api/django.json": [{"cycle": "5.2", "eol": "2030-04-30"}, {"cycle": "4.2", "eol": soon}],
                 "/api/rails.json": [{"cycle": "8.0", "eol": False}, {"cycle": "6.1", "eol": "2024-10-01"}]}
    assert client.put("/api/runtime", headers=key, json={"language": "node", "version": "20.11.1"}).status_code == 200
    assert client.put("/api/runtime", headers=key, json={"language": "cobol", "version": "1"}).status_code == 400
    client.put("/api/dependencies", headers=key, json={"packages": [
        {"ecosystem": "PyPI", "name": "django", "version": "4.2.11", "direct": True},
        {"ecosystem": "RubyGems", "name": "rails", "version": "6.1.7", "direct": True},
        {"ecosystem": "npm", "name": "redis", "version": "4.6.0", "direct": True},  # a client library, not the server
        {"ecosystem": "runtime", "name": "node", "version": "18.0.0"},  # from a file: the running server's report wins
    ]})
    versions(site, db)
    found = {i.key: i for i in incidents(db)}
    assert set(found) == {"nodejs", "django", "rails"}
    node = found["nodejs"]
    assert (node.kind, node.level, node.title) == ("eol", "look", "Node.js 20 no longer gets security fixes")
    assert node.what.startswith("Your app runs Node.js 20.11.1. Support for Node.js 20 ended on 30 Apr 2026")
    assert node.todo == "Ask your coding agent to move to Node.js 26, run the tests, and deploy."
    assert found["django"].title == "Django 4.2 stops getting security fixes in 30 days"
    assert found["rails"].title == "Rails 6.1 no longer gets security fixes"
    # Three findings in one pass are one email, not three.
    email, = emails(db)
    assert email.subject == "Needs a look: 3 things about the versions your app runs on on yourapp.com"
    assert "- Rails 6.1 no longer gets security fixes" in email.body and "What to do: Ask your coding agent to move to Node.js 26" in email.body

    # After the upgrade the problem closes by itself.
    client.put("/api/runtime", headers=key, json={"language": "node", "version": "26.13.1"})
    versions(site, db)
    assert found["nodejs"].ended_at is not None and found["django"].ended_at is None


def test_a_supported_node_that_missed_security_releases_needs_a_look_and_python_behind_is_only_listed(client, db, project, key, site):
    site.json = {
        "/api/nodejs.json": NODE,
        "/api/python.json": [{"cycle": "3.12", "eol": "2028-10-31", "latest": "3.12.8"}],
        "/dist/index.json": [{"version": "v24.9.0", "security": True}, {"version": "v24.8.1", "security": False},
                             {"version": "v24.8.0", "security": True}, {"version": "v24.7.0", "security": True},
                             {"version": "v26.1.0", "security": True}],
    }
    client.put("/api/runtime", headers=key, json={"language": "node", "version": "24.7.0"})
    client.put("/api/runtime", headers=key, json={"language": "python", "version": "3.12.1"})
    versions(site, db)
    incident, = incidents(db)
    assert (incident.kind, incident.title) == ("runtime", "Node.js 24.7.0 is missing 2 security releases")
    assert "the newest Node.js 24 is 24.9.0" in incident.what
    assert "move to Node.js 24.9.0" in incident.todo
    db.add(view(project, "a"))
    db.commit()
    assert "Python 3.12.1 is behind 3.12.8, the newest release of the same line." in page_text(client, project)


def test_deprecated_packages_and_packages_behind(client, db, project, key, site):
    site.json = {
        "/v3alpha/versionbatch": {"responses": [
            dev("request", "2.88.2", "request has been deprecated, see https://github.com/request/request/issues/3142"),
            dev("inflight", "1.0.6", "This module is not supported, and leaks memory."),
            dev("next", "14.2.3"), dev("django", "4.2.11", system="PYPI"),
            dev("github.com/gin-gonic/gin", "v1.7.0", system="GO"),
            {"request": {"versionKey": {"system": "NPM", "name": "private-thing", "version": "1.0.0"}}},  # unknown to deps.dev
        ]},
        "/v3/systems/npm/packages/next": {"versions": [{"versionKey": {"version": "14.2.3"}}, {"versionKey": {"version": "16.4.6"}, "isDefault": True}]},
        "/v3/systems/npm/packages/request": {"versions": [{"versionKey": {"version": "2.88.2"}, "isDefault": True}]},
        "/v3/systems/pypi/packages/django": {"versions": [{"versionKey": {"version": "5.2.8"}, "isDefault": True}]},
        "/v3/systems/go/packages/github.com%2Fgin-gonic%2Fgin": {"versions": [{"versionKey": {"version": "v1.10.0"}, "isDefault": True}]},
    }
    packages = [
        {"ecosystem": "npm", "name": "request", "version": "2.88.2", "direct": True},
        {"ecosystem": "npm", "name": "inflight", "version": "1.0.6"},
        {"ecosystem": "npm", "name": "next", "version": "14.2.3", "direct": True},
        {"ecosystem": "npm", "name": "private-thing", "version": "1.0.0"},
        {"ecosystem": "PyPI", "name": "django", "version": "4.2.11", "direct": True},
        {"ecosystem": "Go", "name": "github.com/gin-gonic/gin", "version": "1.7.0", "direct": True},
        {"ecosystem": "Packagist", "name": "laravel/framework", "version": "11.0.0", "direct": True},  # not on deps.dev
    ]
    client.put("/api/dependencies", headers=key, json={"packages": packages})
    versions(site, db)

    # A deprecated package the project names itself needs a look; one pulled in by others is only listed.
    incident, = incidents(db)
    assert (incident.kind, incident.key, incident.title) == ("deprecated", "npm/request", "request is deprecated")
    assert incident.what == ("The authors of request say of version 2.88.2: "
                             "“request has been deprecated, see https://github.com/request/request/issues/3142”.")
    deps = {d.name: d for d in db.scalars(select(Dependency))}
    assert deps["inflight"].deprecated == "This module is not supported, and leaks memory."
    assert deps["next"].deprecated is None and deps["private-thing"].facts_checked_at is not None
    assert {n: d.latest for n, d in deps.items()} == {
        "request": "2.88.2", "inflight": None, "next": "16.4.6", "private-thing": None, "django": "5.2.8",
        "github.com/gin-gonic/gin": "1.10.0", "laravel/framework": None}

    db.add(view(project, "a"))
    db.commit()
    text = page_text(client, project)
    assert "2 packages are a major version behind: next 14.2.3 → 16.4.6, django 4.2.11 → 5.2.8." in text
    assert "1 package your app pulls in indirectly is deprecated by its authors." in text
    status = client.get("/api/status", headers=key).json()["text"]
    assert "- inflight 1.0.6 (pulled in indirectly) is deprecated: This module is not supported, and leaks memory." in status

    # Sending the same list again keeps what is known; an upgraded package is looked up afresh.
    site.json["/v3alpha/versionbatch"] = {"responses": []}
    packages[0] = {"ecosystem": "npm", "name": "got", "version": "14.0.0", "direct": True}
    client.put("/api/dependencies", headers=key, json={"packages": packages})
    versions(site, db)
    db.expire_all()
    deps = {d.name: d for d in db.scalars(select(Dependency))}
    assert "request" not in deps and deps["inflight"].deprecated and deps["next"].latest == "16.4.6"
    assert incidents(db)[0].ended_at is not None  # request is gone, so its problem closes
