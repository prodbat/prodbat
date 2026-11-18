import datetime as dt
import re

from fastapi.testclient import TestClient

from prodbat_server.app import create_app
from prodbat_server.models import Pageview
from prodbat_server.setup_emails import send_setup_emails

from .helpers import BROWSER, NOW, SETTINGS, emails, evaluate, failure, http_client, minutes_ago, page_text, run, \
    run_checks
from .test_ingest import post


def setup_emails(db, now=None):
    return run(lambda sessions: send_setup_emails(sessions, SETTINGS, now), db)


def test_the_page_starts_as_a_checklist_with_two_steps_already_done(client, db, project):
    text = page_text(client, project)
    assert "Setting up yourapp.com" in text
    assert "Your agent created the project" in text and "Alerts have an address to go to" in text
    assert "Your site sends its first visit Happens after your agent deploys the script tag." in text
    assert "Send a test alert" in text
    assert "Still waiting" not in text  # only after a quarter of an hour

    project.created_at = minutes_ago(20)
    db.commit()
    assert "Run npx prodbat verify and fix what it reports." in page_text(client, project)


def test_the_first_visit_and_the_first_server_report_are_recorded_once(client, db, project, key):
    assert post(client, {"t": "pv", "u": "https://yourapp.com/"}).status_code == 202
    db.refresh(project)
    first = project.first_data_at
    assert first is not None and project.first_server_at is None

    post(client, {"t": "pv", "u": "https://yourapp.com/pricing"})
    client.put("/api/runtime", headers=key, json={"language": "python", "version": "3.12.4"})
    db.refresh(project)
    assert project.first_data_at == first and project.first_server_at is not None

    # Reading the project's status from the CLI is not the server reporting in.
    project.first_server_at = None
    db.commit()
    client.get("/api/status", headers=key)
    db.refresh(project)
    assert project.first_server_at is None


def test_once_data_arrives_whats_left_is_a_card_of_lines_for_the_agent(client, db, project):
    project.first_data_at = minutes_ago(5)
    db.add(Pageview(project_id=project.id, visitor="a", path="/", source=None, source_kind="direct",
                    ts=minutes_ago(5)))
    db.commit()
    text = page_text(client, project)
    assert "Finish setting up" in text and "3 of 8 done" in text
    assert "Payments: forward your Stripe events Run `npx prodbat suggestions S-01` and follow it." in text
    assert "Nothing has broken since" in text


def test_you_are_live_goes_once_after_the_first_visit_and_nudge_goes_once_after_a_quiet_day(db, project):
    assert setup_emails(db) == 0  # no data yet, and not a day old

    project.first_data_at = minutes_ago(2)
    db.commit()
    assert setup_emails(db) == 1
    live = emails(db)[-1]
    assert live.subject == "Prodbat is watching yourapp.com"
    assert "loaded from outside every 5 minutes" in live.body and "#test-alert" in live.body
    assert setup_emails(db) == 0

    project.first_data_at, project.created_at = None, NOW - dt.timedelta(hours=25)
    db.commit()
    assert setup_emails(db) == 1
    assert emails(db)[-1].subject == "Prodbat hasn't heard from yourapp.com yet"
    assert "Run npx prodbat verify and fix what it reports." in emails(db)[-1].body
    assert setup_emails(db) == 0


def test_a_paused_project_is_not_checked_stored_evaluated_or_emailed(client, db, project, site):
    project.paused_at = NOW
    db.add_all([failure(project, v, minutes_ago(3)) for v in "abc"])
    db.commit()

    assert post(client, {"t": "pv", "u": "https://yourapp.com/"}).status_code == 202
    assert db.query(Pageview).count() == 0
    assert run_checks(db, site) == 0
    evaluate(db)
    assert emails(db) == []
    assert "Monitoring is paused" in page_text(client, project)


def test_each_project_has_its_own_check_interval(db, project, site):
    project.check_every_seconds = 60
    db.commit()
    run_checks(db, site)
    db.refresh(project)
    assert dt.timedelta(seconds=50) < project.next_check_at - dt.datetime.now(dt.UTC) <= dt.timedelta(seconds=60)


def test_an_extension_adds_routes_guards_pages_and_runs_on_the_worker(db, project, site):
    from . import sample_extension

    settings = SETTINGS.model_copy(update={"extensions": "tests.sample_extension"})
    with TestClient(create_app(settings, http=http_client(site)), headers={"user-agent": BROWSER}) as client:
        assert client.get("/llms.txt").text == "from the extension"
        page = f"/d/{project.page_token}"
        refused = client.get(page, follow_redirects=False)
        assert refused.status_code == 303 and refused.headers["location"] == "/login"
        assert client.get(page, headers={"x-signed-in": "yes"}).status_code == 200

    from prodbat_server.app import load_extensions
    module, = [m for m in load_extensions(settings) if hasattr(m, "tick")]
    run(lambda sessions: module.tick(sessions, settings, None))
    assert sample_extension.ticks == ["https://pb.example"]


def test_render_style_database_addresses_are_accepted():
    from prodbat_server.config import Settings

    assert Settings(database_url="postgres://u:p@h/db").database_url == "postgresql+psycopg://u:p@h/db"
    assert Settings(database_url="postgresql://u:p@h/db").database_url == "postgresql+psycopg://u:p@h/db"


def test_a_new_secret_key_is_shown_once_and_the_old_one_stops_working(client, db, project, key):
    assert client.get("/api/status", headers=key).status_code == 200
    page = client.post(f"/d/{project.page_token}/key")
    new = re.search(r"<pre id=\"key\">(pbs_[^<]+)</pre>", page.text)[1]
    assert client.get("/api/status", headers=key).status_code == 401
    assert client.get("/api/status", headers={"authorization": f"Bearer {new}"}).status_code == 200
    assert "Replace PRODBAT_KEY in .env" in page.text


def test_a_daily_event_cap_drops_the_rest_quietly(client, db, project):
    project.daily_event_cap = 2
    db.commit()
    codes = [post(client, {"t": "pv", "u": "https://yourapp.com/"}).status_code for _ in range(4)]
    assert codes == [202, 202, 202, 202]
    assert db.query(Pageview).count() == 2


def test_the_monday_email_goes_at_eight_in_the_owners_time_zone(client, db, project):
    from prodbat_server.weekly import send_weekly
    from prodbat_server.words import clock

    project.created_at = NOW - dt.timedelta(days=30)
    db.add(Pageview(project_id=project.id, visitor="a", path="/", source=None, source_kind="direct",
                    ts=NOW - dt.timedelta(days=2)))
    db.commit()  # the server's own update must not wait on this session's lock
    assert client.post(f"/d/{project.page_token}/timezone", data={"tz": "Mars/Olympus"}).status_code == 400
    client.post(f"/d/{project.page_token}/timezone", data={"tz": "America/Los_Angeles"})
    db.refresh(project)
    assert project.timezone == "America/Los_Angeles"

    monday_9_utc = dt.datetime(2026, 11, 23, 9, 0, tzinfo=dt.UTC)  # 01:00 in Los Angeles: too early there
    assert run(lambda s: send_weekly(s, SETTINGS, monday_9_utc), db) == 0
    monday_17_utc = dt.datetime(2026, 11, 23, 17, 0, tzinfo=dt.UTC)  # 09:00 in Los Angeles
    assert run(lambda s: send_weekly(s, SETTINGS, monday_17_utc), db) == 1
    assert run(lambda s: send_weekly(s, SETTINGS, monday_17_utc + dt.timedelta(hours=2)), db) == 0, "once a week"
    assert clock(monday_17_utc, "America/Los_Angeles") == "09:00 on 23 Nov"
    assert clock(monday_17_utc, None) == "17:00 UTC on 23 Nov"
