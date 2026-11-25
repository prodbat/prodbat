import datetime as dt

from sqlalchemy import select

from prodbat_server.mailer import build, send_pending
from prodbat_server.models import BrowserError, Incident, Job, ServerError

from .helpers import NOW, SETTINGS, emails, evaluate, failure, minutes_ago, page_and_week, page_text, run, view


def incidents(db):
    db.expire_all()
    return db.scalars(select(Incident).order_by(Incident.id)).all()


def test_a_route_failing_for_three_visitors_is_broken_and_emailed_once(client, db, project):
    db.add_all([failure(project, v, minutes_ago(m)) for v, m in [("a", 9), ("b", 6), ("c", 3), ("c", 2)]])
    evaluate(db)
    evaluate(db)  # a second pass must not send a second email

    incident, = incidents(db)
    assert (incident.kind, incident.key, incident.level) == ("route", "POST /api/save", "broken")
    email, = emails(db)
    assert email.to == "you@example.com"
    assert email.subject == "Broken: POST /api/save is failing on yourapp.com"
    # The four questions, in order.
    order = [email.body.index(q) for q in ("What broke:", "Since when:", "Who is affected:", "What to do:")]
    assert order == sorted(order)
    assert "answered with an error (500) for 3 visitors in the last 15 minutes" in email.body
    assert f"Run `npx prodbat task T{incident.id}`" in email.body and f"`npx prodbat done T{incident.id}`" in email.body
    assert f"https://pb.example/d/{project.page_token}" in email.body

    text = page_and_week(client, project)
    assert "Your site is broken right now." in text
    assert "POST /api/save is failing." in text
    assert "Copy the fix for your agent" in text and "What your agent will be told" in text


def test_when_the_failures_stop_it_closes_with_a_working_again_in_the_same_thread(db, project):
    db.add_all([failure(project, v, minutes_ago(44 - i)) for i, v in enumerate("abc")])
    old = Incident(project_id=project.id, kind="route", key="POST /api/save", level="broken",
                   started_at=minutes_ago(44), title="POST /api/save is failing", what="w", who="w",
                   message_id="<first@pb.example>")
    db.add(old)
    evaluate(db)

    incident, = incidents(db)
    assert incident.ended_at is not None
    email, = emails(db)
    assert email.subject == "Working again: POST /api/save is failing on yourapp.com"
    assert email.in_reply_to == "<first@pb.example>"
    assert "No visitor has hit this error for 30 minutes." in email.body
    message = build(email, SETTINGS)
    assert message["References"] == "<first@pb.example>"
    assert message["From"] == "Prodbat <alerts@pb.example>"

    # The same old failures must not reopen it as "needs a look".
    evaluate(db)
    assert len(incidents(db)) == 1 and len(emails(db)) == 1


def test_two_visitors_over_a_day_need_a_look_and_a_third_soon_after_makes_it_broken(db, project):
    db.add_all([failure(project, "a", minutes_ago(300)), failure(project, "b", minutes_ago(200))])
    evaluate(db)
    assert [(i.level, i.title) for i in incidents(db)] == [("look", "POST /api/save answered with an error (500)")]
    assert [e.subject for e in emails(db)] == ["Needs a look: POST /api/save answered with an error (500) on yourapp.com"]

    db.add_all([failure(project, v, minutes_ago(m)) for v, m in [("c", 5), ("d", 4), ("e", 3)]])
    evaluate(db)
    incident, = incidents(db)
    assert incident.level == "broken"
    first, second = emails(db)
    assert second.subject.startswith("Broken: POST /api/save is failing")
    assert second.in_reply_to == first.message_id


def test_one_visitor_hitting_one_error_is_listed_but_not_a_problem(client, db, project):
    db.add_all([view(project, "a"), failure(project, "a", minutes_ago(5))])
    evaluate(db)
    assert incidents(db) == [] and emails(db) == []
    text = page_and_week(client, project)
    assert "Everything is working." in text
    assert "POST /api/save answered with an error (500). 1 visitor affected" in text


def test_a_browser_error_seen_by_two_visitors_needs_a_look(client, db, project):
    db.add_all([BrowserError(project_id=project.id, visitor=v, path="/signup", fingerprint="f1",
                             message="x is not defined", file="https://yourapp.com/assets/app.js", line=12, col=4,
                             stack="", ts=minutes_ago(120)) for v in "ab"])
    evaluate(db)
    incident, = incidents(db)
    assert (incident.level, incident.title) == ("look", "An error in your code on /signup")
    assert incident.what == "“x is not defined” (app.js, line 12)."
    assert "Something needs a look." in page_and_week(client, project)


def test_this_is_fine_stop_telling_me(client, db, project):
    db.add_all([failure(project, v, minutes_ago(3)) for v in "abc"])
    evaluate(db)
    incident, = incidents(db)

    r = client.post(f"/d/{project.page_token}/mute/{incident.id}", follow_redirects=False)
    assert r.status_code == 303
    assert incidents(db)[0].ended_at is not None
    assert len(emails(db)) == 1  # muting is not "working again"

    db.add_all([failure(project, v, minutes_ago(1)) for v in "def"])
    evaluate(db)
    assert len(incidents(db)) == 1 and len(emails(db)) == 1  # stays quiet
    assert "Everything is working." in page_and_week(client, project)
    assert client.post(f"/d/wrong/mute/{incident.id}").status_code == 404


def test_a_server_error_is_reported_with_its_cause_and_a_burst_is_broken(db, project):
    def error(ts):
        return ServerError(project_id=project.id, fingerprint="s1", kind="KeyError", message="'price_id'",
                           route="POST /api/save", where="billing/views.py:88 in create_session", trace="t", ts=ts)

    db.add(error(minutes_ago(100)))
    evaluate(db)
    incident, = incidents(db)
    assert incident.level == "look"
    assert "Cause: KeyError: 'price_id' at billing/views.py:88 in create_session." in incident.what

    db.add_all([error(minutes_ago(m)) for m in (5, 4, 3, 2, 1)])
    evaluate(db)
    assert incidents(db)[0].level == "broken"
    assert emails(db)[-1].subject == "Broken: POST /api/save is failing on yourapp.com"


def test_a_job_that_stops_checking_in_is_broken_until_it_checks_in_again(client, db, project, key):
    db.add(Job(project_id=project.id, name="nightly-email", every_seconds=86400,
               last_ping_at=NOW - dt.timedelta(hours=37)))
    evaluate(db)
    incident, = incidents(db)
    assert (incident.kind, incident.level, incident.title) == ("job", "broken", "Your nightly-email job did not run")
    assert "It normally checks in every 24 hours." in incident.what

    assert client.post("/api/ping/nightly-email", headers=key).json()["expected_every_seconds"] == 86400
    evaluate(db)
    assert incidents(db)[0].ended_at is not None
    assert emails(db)[-1].subject == "Working again: Your nightly-email job did not run on yourapp.com"
    assert "The job has checked in again." in emails(db)[-1].body


def test_the_outbox_is_sent_once_and_a_failing_mail_server_is_retried(db, project):
    db.add_all([failure(project, v, minutes_ago(3)) for v in "abc"])
    evaluate(db)
    delivered = []

    def broken_server(message, settings):
        raise ConnectionRefusedError("mail server is down")

    assert run(lambda s: send_pending(s, SETTINGS, deliver=broken_server), db) == 0
    email, = emails(db)
    assert email.sent_at is None and email.attempts == 1 and "ConnectionRefusedError" in email.last_error

    assert run(lambda s: send_pending(s, SETTINGS, deliver=lambda m, _: delivered.append(m)), db) == 1
    assert run(lambda s: send_pending(s, SETTINGS, deliver=lambda m, _: delivered.append(m)), db) == 0
    assert [m["Subject"] for m in delivered] == ["Broken: POST /api/save is failing on yourapp.com"]
    assert emails(db)[0].sent_at is not None


def test_a_project_without_an_address_gets_problems_but_no_email(db, project):
    project.alert_email = None
    db.add_all([failure(project, v, minutes_ago(3)) for v in "abc"])
    evaluate(db)
    assert len(incidents(db)) == 1 and emails(db) == []
