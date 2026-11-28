import datetime as dt

from sqlalchemy import select

from prodbat_server.checks import CHECK_EVERY, apply_expiry, refresh_expiry
from prodbat_server.models import Incident, Project, SiteCheck
from prodbat_server.words import ago, compared, every, lasted, money

from .helpers import NOW, SETTINGS, days_ago, emails, http_client, page_and_week, page_text, run, run_checks, view


def make_due(db, project):
    project.next_check_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    db.commit()


# ---- the page -----------------------------------------------------------

def test_page_needs_the_secret_link(client, project):
    assert client.get("/d/not-the-token").status_code == 404
    r = client.get(f"/d/{project.page_token}")
    assert r.headers["x-robots-tag"] == "noindex"
    assert r.headers["cache-control"] == "no-store"


def test_before_any_data_it_says_it_is_waiting_and_shows_the_script_tag(client, project):
    text = page_and_week(client, project)
    assert "Waiting for your first visitor." in text
    assert 'data-site="pb_test"' in text
    assert "Everything is working." not in text


def test_a_quiet_week_says_everything_is_working_with_numbers_and_lists(client, db, project):
    db.add_all([
        view(project, "a", "/", "Google", "search", days_ago(2)),
        view(project, "a", "/pricing", None, "internal", days_ago(2) + dt.timedelta(minutes=1)),
        view(project, "b", "/", "ChatGPT", "ai", days_ago(1)),
        view(project, "c", "/pricing", None, "direct"),
        view(project, "old", "/", "Google", "search", days_ago(9)),  # last week: not counted
    ])
    db.commit()
    text = page_and_week(client, project)
    assert "Everything is working." in text
    assert "Last heard from your site 30 minutes ago." in text
    assert "Visitors 3" in text
    assert "Page views 4" in text
    assert "Payments – Stripe is not connected yet" in text
    assert "Visitors who saw an error 0 of 3 visitors" in text
    assert "Nothing has broken since" in text
    assert "/ 2" in text and "/pricing 2" in text
    # Once a problem has come and gone, the page says when, not since the project began.
    db.add(Incident(project_id=project.id, kind="server", key="s1", level="look", title="t", what="w", who="w",
                    started_at=NOW - dt.timedelta(hours=3), ended_at=NOW - dt.timedelta(hours=2)))
    db.commit()
    later = page_and_week(client, project)
    assert "Nothing has broken since" not in later
    assert "Nothing is wrong right now. The last problem was fixed 2 hours ago." in later
    # Each visitor counts once, under where they first arrived from.
    assert "Google 1" in text
    assert "ChatGPT AI assistant 1" in text
    assert "Typed the address or unknown 1" in text
    assert "last week" not in text  # a new project has nothing to compare with
    # The first suggestion that applies is offered as a line for the agent.
    assert "Run `npx prodbat suggestions S-01` and follow it." in text
    assert "Alerts go to you@example.com." in page_text(client, project, "settings")


def test_send_me_a_test_alert(client, db, project):
    r = client.post(f"/d/{project.page_token}/test-alert", follow_redirects=False)
    assert r.status_code == 303
    email, = emails(db)
    assert email.subject == "Test: this is what a Prodbat alert for yourapp.com looks like"
    assert "A test alert is on its way to you@example.com." in client.get(r.headers["location"]).text


def test_wording_helpers():
    assert ago(NOW - dt.timedelta(seconds=1), NOW) == "1 second ago"
    assert ago(NOW - dt.timedelta(hours=26), NOW) == "1 day ago"
    assert compared(412, 374, True) == "38 more than last week"
    assert compared(90, 100, True) == "10 fewer than last week"
    assert compared(101, 100, True) == "about the same as last week"
    assert compared(5, 0, False) == ""
    assert lasted(7 * 60) == "7 minutes" and lasted(5 * 3600) == "5 hours"
    assert every(86400) == "every 24 hours" and every(900) == "every 15 minutes"
    assert money(1900, "usd") == "$19" and money(1950, "eur") == "€19.50" and money(500, "jpy") == "500 JPY"


# ---- loading the site from outside --------------------------------------

def test_a_site_that_loads_is_recorded_and_not_checked_again_for_five_minutes(db, project, site):
    assert run_checks(db, site) == 1
    check = db.scalars(select(SiteCheck)).one()
    assert check.ok is True and check.status == 200
    assert project.next_check_at > dt.datetime.now(dt.UTC) + CHECK_EVERY - dt.timedelta(seconds=30)
    assert run_checks(db, site) == 0
    assert db.scalars(select(Incident)).all() == []


def test_one_failed_load_followed_by_a_good_one_is_not_a_problem(db, project, site):
    site.answers = [502, 200]
    run_checks(db, site)
    assert db.scalars(select(SiteCheck)).one().ok is True
    assert db.scalars(select(Incident)).all() == [] and emails(db) == []


def test_two_failed_loads_are_broken_and_emailed_and_a_good_load_says_working_again(client, db, project, site):
    site.answers = [502]
    run_checks(db, site)
    incident = db.scalars(select(Incident)).one()
    assert (incident.kind, incident.level, incident.ended_at) == ("down", "broken", None)
    assert incident.what == "yourapp.com answered with an error (502). We tried twice from outside."
    assert emails(db)[0].subject == "Broken: Your site is not loading on yourapp.com"
    assert "Who is affected: Everyone who visits." in emails(db)[0].body

    # Broken shows even though no visitor data has ever arrived.
    text = page_and_week(client, project)
    assert "Your site is broken right now." in text and "Your site is not loading." in text

    make_due(db, project)
    site.answers = ["timeout"]
    run_checks(db, site)  # still down: no second problem, no second email
    assert len(db.scalars(select(Incident)).all()) == 1 and len(emails(db)) == 1

    make_due(db, project)
    site.answers = [200]
    run_checks(db, site)
    assert incident.ended_at is not None
    again = emails(db)[1]
    assert again.subject == "Working again: Your site is not loading on yourapp.com"
    assert again.in_reply_to == emails(db)[0].message_id
    assert "Your site is loading again: we loaded it from outside in" in again.body


def test_a_site_that_refuses_to_talk_to_us_is_neither_up_nor_down(db, project, site):
    site.answers = [403]
    run_checks(db, site)
    assert db.scalars(select(SiteCheck)).one().ok is None
    assert db.scalars(select(Incident)).all() == []


def test_a_past_outage_is_listed_under_what_happened(client, db, project):
    start = NOW - dt.timedelta(days=3)
    db.add(view(project, "a"))
    db.add(Incident(project_id=project.id, kind="down", level="broken", started_at=start,
                    ended_at=start + dt.timedelta(minutes=7), title="Your site is not loading", what="w", who="w"))
    db.commit()
    text = page_and_week(client, project)
    assert "Everything is working." in text
    assert f"{start.day} {start:%b} Your site did not load for 7 minutes." in text


def test_a_new_site_that_loads_but_has_no_script_yet_is_still_waiting(client, db, project, site):
    run_checks(db, site)
    text = page_and_week(client, project)
    assert "Waiting for your first visitor." in text and "Your site loads from outside." in text


# ---- certificate and domain ---------------------------------------------

def test_expiry_dates_are_looked_up_and_a_certificate_about_to_run_out_needs_a_look(client, db, project, site):
    soon = dt.datetime.now(dt.UTC) + dt.timedelta(days=5, hours=1)
    site.rdap = {"events": [{"eventAction": "expiration",
                             "eventDate": (dt.datetime.now(dt.UTC) + dt.timedelta(days=61, hours=1)).isoformat()}]}

    async def go(sessions):
        async with http_client(site) as http:
            await refresh_expiry(sessions, http, SETTINGS, certificate_expiry=lambda domain: soon)

    run(go, db)
    assert project.cert_expires_at == soon
    incident = db.scalars(select(Incident)).one()
    assert (incident.kind, incident.level) == ("cert", "look")
    assert incident.title == "Your secure connection certificate runs out in 5 days"
    assert emails(db)[0].subject.startswith("Needs a look: Your secure connection certificate runs out in 5 days")

    db.add(view(project, "a"))
    db.commit()
    assert "Your domain needs renewing in 61 days." in page_and_week(client, project)

    # Once it has been renewed the problem closes by itself, without an email.
    async def renewed(sessions):
        async with sessions() as session:
            p = await session.get(Project, project.id)
            p.cert_expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(days=80)
            await apply_expiry(session, p, SETTINGS, dt.datetime.now(dt.UTC))
            await session.commit()

    run(renewed, db)
    assert incident.ended_at is not None and len(emails(db)) == 1
