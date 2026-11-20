import datetime as dt

from sqlalchemy import select, update

from prodbat_server import api
from prodbat_server.advisories import fixed_version
from prodbat_server.models import Dependency, Incident, Job, PackageFacts, Payment, Project, ServerError
from prodbat_server.stripe_events import product_name
from prodbat_server.weekly import send_weekly

from .helpers import NOW, SETTINGS, days_ago, emails, evaluate, failure, http_client, minutes_ago, page_text, run, run_jobs, view

ADMIN = {"authorization": "Bearer admin"}


# ---- creating a project -------------------------------------------------

def test_a_project_is_created_with_the_admin_token_and_its_key_then_works(client, db):
    r = client.post("/api/projects", json={"domain": "https://www.NewApp.com/", "email": "me@newapp.com"}, headers=ADMIN)
    assert r.status_code == 201
    made = r.json()
    assert made["domain"] == "newapp.com" and made["server"] == "https://pb.example"
    assert made["script_tag"] == f'<script defer src="https://pb.example/p.js" data-site="{made["site_key"]}"></script>'
    assert made["secret_key"].startswith("pbs_")
    project = db.scalars(select(Project)).one()
    assert made["secret_key"] not in (project.secret_hash, project.key, project.page_token)  # only its hash is kept

    status = client.get("/api/status", headers={"authorization": f"Bearer {made['secret_key']}"})
    assert status.status_code == 200 and status.json()["status"] == "waiting"


def test_creating_a_project_is_refused_without_the_token_or_with_bad_input(client, project):
    assert client.post("/api/projects", json={"domain": "a.com"}).status_code == 401
    assert client.post("/api/projects", json={"domain": "a.com"}, headers={"authorization": "Bearer nope"}).status_code == 401
    assert client.post("/api/projects", json={"domain": "not a domain"}, headers=ADMIN).status_code == 400
    assert client.post("/api/projects", json={"domain": "a.com", "email": "nope"}, headers=ADMIN).status_code == 400
    again = client.post("/api/projects", json={"domain": "yourapp.com"}, headers=ADMIN)
    assert again.status_code == 409 and "already set up" in again.json()["detail"]


def test_everything_else_needs_the_projects_own_key(client, project):
    for method, path in [("get", "/api/status"), ("post", "/api/verify"), ("get", "/api/suggestions"),
                         ("post", "/api/ping/job"), ("post", "/api/test-alert")]:
        assert getattr(client, method)(path).status_code == 401
        assert getattr(client, method)(path, headers={"authorization": "Bearer pbs_wrong"}).status_code == 401


# ---- status and verify --------------------------------------------------

def test_status_gives_an_agent_the_problem_and_the_evidence(client, db, project, key):
    db.add(view(project, "a"))
    db.add_all([failure(project, v, minutes_ago(m)) for v, m in [("a", 9), ("b", 6), ("c", 3)]])
    evaluate(db)
    status = client.get("/api/status", headers=key).json()
    assert status["status"] == "broken"
    problem = status["problems"][0]
    assert problem["title"] == "POST /api/save is failing."
    assert len(problem["evidence"]) == 3 and "POST /api/save -> 500  (visitor was on /editor)" in problem["evidence"][0]
    assert status["text"].startswith("# yourapp.com: Your site is broken right now.")
    assert "## BROKEN: POST /api/save is failing." in status["text"]
    assert "Suggestion:" in status["text"]


def test_verify_passes_when_the_site_loads_and_carries_the_script(client, project, key):
    result = client.post("/api/verify", headers=key).json()
    assert result["ok"] is True
    assert [c["ok"] for c in result["checks"]] == [True, True, None, None]
    assert "ok   The script is on your pages. The tag is in the page." in result["text"]
    assert "printed in its log instead of sent" in result["text"]  # no mail server configured in tests


def test_verify_fails_with_the_fix_when_the_script_is_missing_or_the_site_is_down(client, project, key, site):
    site.body = "<html>no script here</html>"
    result = client.post("/api/verify", headers=key).json()
    assert result["ok"] is False
    assert 'FAIL The script is on your pages.' in result["text"] and 'data-site="pb_test"' in result["text"]

    site.answers = [503]
    result = client.post("/api/verify", headers=key).json()
    assert "FAIL Your site loads. yourapp.com answered with an error (503)." in result["text"]


def test_the_free_check_needs_no_key_and_says_what_it_found(client, site, monkeypatch):
    good_until = dt.datetime(2031, 2, 14, tzinfo=dt.UTC)
    monkeypatch.setattr(api, "certificate_expiry", lambda domain: good_until)
    site.body = '<script src="https://js.stripe.com/v3"></script><script src="/_next/static/a.js"></script>'
    result = client.get("/api/check", params={"url": "https://anysite.com/pricing"}).json()
    assert result["domain"] == "anysite.com"
    assert any(line.startswith("Your site loads (") for line in result["working"])
    assert "Secure connection works; the certificate is good until 14 Feb 2031." in result["working"]
    assert "Your domain is paid up until 3 Aug 2030." in result["working"]
    assert result["found"] == ["Hosted on Vercel.", "Built with Next.js.", "Takes payments with Stripe."]
    assert client.get("/api/check", params={"url": "localhost"}).status_code == 400


# ---- payments -----------------------------------------------------------

def invoice(id="in_1", amount=1900, description="1 × Pro plan (at $19.00 / month)", created=None):
    return {"type": "invoice.paid", "created": int((created or NOW).timestamp()) - 3600, "data": {"object": {
        "id": id, "amount_paid": amount, "currency": "usd",
        "lines": {"data": [{"description": description}]}}}}


def test_forwarded_stripe_events_become_what_sells(client, db, project, key):
    db.add_all([view(project, "buyer", "/", "ChatGPT", "ai"), view(project, "buyer", "/pricing", None, "internal", ref="abc123")])
    db.commit()
    assert client.post("/api/stripe", json=invoice(), headers=key).json() == {"ok": True, "payments": 1}
    client.post("/api/stripe", json={**invoice(), "type": "invoice.payment_succeeded"}, headers=key)  # same invoice
    client.post("/api/stripe", json=invoice("in_2"), headers=key)
    client.post("/api/stripe", headers=key, json={"type": "checkout.session.completed", "created": int(NOW.timestamp()) - 60,
                "data": {"object": {"id": "cs_1", "mode": "payment", "payment_status": "paid", "amount_total": 9900,
                                    "currency": "usd", "client_reference_id": "abc123",
                                    "metadata": {"product": "Lifetime deal"}}}})
    # Not payments: a subscription checkout (its invoice is counted) and an unrelated event.
    client.post("/api/stripe", headers=key, json={"type": "checkout.session.completed", "data": {"object": {
        "id": "cs_2", "mode": "subscription", "payment_status": "paid", "amount_total": 1900}}})
    assert client.post("/api/stripe", json={"type": "customer.created", "data": {"object": {}}}, headers=key).json()["payments"] == 0
    assert client.post("/api/stripe", content="not json", headers=key).status_code == 400

    payments = db.scalars(select(Payment).order_by(Payment.stripe_id)).all()
    assert [(p.stripe_id, p.amount, p.product, p.source) for p in payments] == [
        ("cs_1", 9900, "Lifetime deal", "ChatGPT"), ("in_1", 1900, "Pro plan", None), ("in_2", 1900, "Pro plan", None)]

    text = page_text(client, project)
    assert "Payments 3" in text and "Paid $137" in text
    assert "Lifetime deal 1 payment $99" in text and "Pro plan 2 payments $38" in text
    assert "Last payment: 1 minute ago." in text
    assert "S-01" not in text  # payments are connected, so the next suggestion is offered
    assert "Run `npx prodbat suggestions S-02` and follow it." in text


def test_stripe_shows_as_connected_before_the_first_payment(client, db, project, key):
    db.add(view(project, "someone", "/", "ChatGPT", "ai"))
    db.commit()
    text = page_text(client, project)
    assert "Payments show up here once your Stripe webhook forwards its events." in text
    assert "Stripe is not connected yet" in text

    # The agent says it finished the Stripe step; nothing has arrived yet.
    db.execute(update(Project).where(Project.id == project.id).values(setup_waiting={"S-01": NOW.isoformat()}))
    db.commit()
    text = page_text(client, project)
    assert "forwards its events" not in text and "not connected" not in text
    assert "Your coding agent set up Stripe forwarding." in text
    assert "Set up; waiting for the first payment" in text

    # Any forwarded event, even one that is not a payment, shows Stripe is connected.
    client.post("/api/stripe", json={"type": "customer.created", "data": {"object": {}}}, headers=key)
    text = page_text(client, project)
    assert "Stripe is connected. No payments have come in yet" in text
    assert "Stripe is connected; no payments yet" in text and "No payments this week." not in text
    assert "S-01" not in text  # the setup item counts as done


def test_product_names_are_read_from_invoice_lines():
    assert product_name("1 × Pro plan (at $19.00 / month)") == "Pro plan"
    assert product_name("3 x Seats (at $5.00 / month)") == "Seats"
    assert product_name("Setup fee") == "Setup fee"
    assert product_name(None) == "Payment"


# ---- from the site's own server -----------------------------------------

def test_a_job_checks_in_and_says_how_often_it_runs(client, db, project, key):
    assert client.post("/api/ping/nightly-email?every=12h", headers=key).json()["expected_every_seconds"] == 43200
    client.post("/api/ping/nightly-email", headers=key)  # later check-ins need not repeat it
    job = db.scalars(select(Job)).one()
    assert (job.name, job.every_seconds) == ("nightly-email", 43200)
    assert client.post("/api/ping/Bad Name", headers=key).status_code == 400
    assert client.post("/api/ping/x?every=soon", headers=key).status_code == 400
    assert "Job nightly-email: last checked in" in client.get("/api/status", headers=key).json()["text"]


def test_server_errors_are_grouped_by_where_they_happened(client, db, project, key):
    python = {"kind": "KeyError", "message": "'price_id'", "route": "POST /api/save",
              "where": "billing/views.py:88 in create_session", "trace": "Traceback ..."}
    assert client.post("/api/errors", json=python, headers=key).status_code == 202
    client.post("/api/errors", json={**python, "message": "'plan'"}, headers=key)
    node = {"kind": "TypeError", "message": "x is undefined", "route": "GET /api/me",
            "trace": "TypeError: x is undefined\n    at handler (/app/node_modules/next/server.js:1:1)\n"
                     "    at getUser (/app/src/lib/user.ts:42:11)"}
    client.post("/api/errors", json=node, headers=key)
    errors = db.scalars(select(ServerError).order_by(ServerError.id)).all()
    assert errors[0].fingerprint == errors[1].fingerprint != errors[2].fingerprint
    assert errors[2].where == "src/lib/user.ts:42"


def test_suggestions_are_fixed_text_filled_only_with_the_server_and_domain(client, project, key):
    listing = client.get("/api/suggestions", headers=key).json()
    assert [s["id"] for s in listing["suggestions"]] == ["S-01", "S-02", "S-03", "S-04", "S-05", "S-06", "S-07"]
    one = client.get("/api/suggestions/s-02", headers=key).json()
    assert one["title"] == "Watch a scheduled job"
    assert one["instructions"].startswith("Before changing anything, read the relevant code")
    assert 'curl -fsS -m 10 -X POST "https://pb.example/api/ping/nightly-email?every=24h"' in one["instructions"]
    assert "{server}" not in one["text"] and "{domain}" not in client.get("/api/suggestions/S-04", headers=key).json()["text"]
    assert client.get("/api/suggestions/S-99", headers=key).status_code == 404


# ---- security problems in dependencies ----------------------------------

def test_a_serious_fixed_problem_in_a_dependency_needs_a_look_until_it_is_upgraded(client, db, project, key, site):
    packages = [{"ecosystem": "npm", "name": "lodash", "version": "4.17.20", "direct": True},
                {"ecosystem": "npm", "name": "left-pad", "version": "1.3.0"},
                {"ecosystem": "PyPI", "name": "jinja2", "version": "3.1.0"}]
    assert client.put("/api/dependencies", json={"packages": packages}, headers=key).json()["packages"] == 3
    assert client.put("/api/dependencies", json={"packages": [{"ecosystem": "cargo", "name": "x", "version": "1"}]},
                      headers=key).status_code == 400

    def osv(vulnerable):
        site.osv = {
            "/v1/querybatch": {"results": [{"vulns": [{"id": "GHSA-35jh"}]} if vulnerable else {}, {},
                                           {"vulns": [{"id": "GHSA-low"}]}]},
            "/v1/vulns/GHSA-35jh": {"id": "GHSA-35jh", "summary": "Command injection in lodash.",
                                    "database_specific": {"severity": "HIGH"},
                                    "affected": [{"package": {"name": "lodash"}, "ranges": [
                                        {"events": [{"introduced": "0"}, {"fixed": "4.17.21"}]}]}]},
            # No severity label: the score from deps.dev decides (7.4 is high).
            "/v1/vulns/GHSA-2nd": {"id": "GHSA-2nd", "summary": "Prototype pollution in lodash",
                                   "affected": [{"package": {"name": "lodash"}, "ranges": [
                                       {"events": [{"introduced": "0"}, {"fixed": "4.17.23"}]}]}]},
            # Not serious: never mentioned.
            "/v1/vulns/GHSA-low": {"id": "GHSA-low", "summary": "Minor.", "database_specific": {"severity": "LOW"},
                                   "affected": [{"package": {"name": "jinja2"}, "ranges": [{"events": [{"fixed": "3.1.5"}]}]}]},
        }

    osv(vulnerable=True)
    run_jobs(db, site)
    incident = db.scalars(select(Incident)).one()
    assert (incident.kind, incident.key, incident.level) == ("advisory", "npm/lodash", "look")
    assert incident.what == ("Your app uses lodash 4.17.20, which is affected by GHSA-35jh: "
                             "Command injection in lodash. It is fixed in 4.17.21.")
    assert "upgrade lodash to 4.17.21 or later" in emails(db)[0].body

    # A second serious problem with a later fix: the advice moves to the version that clears both.
    site.osv["/v1/querybatch"]["results"][0]["vulns"].append({"id": "GHSA-2nd"})
    site.json["/v3/advisories/GHSA-2nd"] = {"cvss3Score": 7.4}
    # A day later: the site is due again, and so are the facts about its packages.
    project.advisories_checked_at = days_ago(2)
    db.execute(update(PackageFacts).values(advisories_checked_at=days_ago(2)))
    run_jobs(db, site)
    assert incident.title == "lodash has 2 serious security problems"
    assert incident.what.endswith("Version 4.17.23 fixes all of them.") and "4.17.23 or later" in incident.todo
    # The one that is not serious is kept on the package and listed, never emailed.
    assert db.scalars(select(Dependency).where(Dependency.name == "jinja2")).one().issues == [
        "GHSA-low: Minor (low, fixed in 3.1.5)"]
    assert len(emails(db)) == 1
    db.add(view(project, "a"))
    db.commit()
    assert "1 other known security problem in your packages, not rated serious or with no fix yet." in page_text(client, project)
    assert "- jinja2 3.1.0: GHSA-low: Minor (low, fixed in 3.1.5)" in client.get("/api/status", headers=key).json()["text"]
    assert emails(db)[0].subject == "Needs a look: lodash has a serious security problem on yourapp.com"

    # The owner upgrades and the list is sent again: the problem closes.
    packages[0]["version"] = "4.17.21"
    client.put("/api/dependencies", json={"packages": packages}, headers=key)
    osv(vulnerable=False)
    run_jobs(db, site)
    assert incident.ended_at is not None
    assert db.scalars(select(Dependency).where(Dependency.name == "lodash")).one().version == "4.17.21"


def test_the_fixed_version_is_the_lowest_one_above_what_is_in_use():
    vuln = {"affected": [{"package": {"name": "pkg"}, "ranges": [
        {"events": [{"introduced": "0"}, {"fixed": "1.2.9"}]}, {"events": [{"introduced": "2.0.0"}, {"fixed": "2.4.1"}]}]}]}
    assert fixed_version(vuln, "pkg", "2.1.0") == "2.4.1"
    assert fixed_version(vuln, "pkg", "1.0.0") == "1.2.9"
    assert fixed_version(vuln, "pkg", "3.0.0") is None
    assert fixed_version(vuln, "other", "1.0.0") is None


# ---- the weekly email ---------------------------------------------------

def test_the_weekly_email_leads_with_their_numbers_and_goes_out_once(client, db, project, key):
    # The most recent Monday at 09:00 UTC; everything else is placed relative to it.
    monday = (NOW - dt.timedelta(days=NOW.weekday())).replace(hour=9, minute=0)
    monday -= dt.timedelta(days=7) if monday > NOW else dt.timedelta(0)
    project.created_at = monday - dt.timedelta(days=20)
    start = monday - dt.timedelta(days=3)
    db.add_all([view(project, v, "/", "Google", "search", monday - dt.timedelta(days=2)) for v in "abcde"])
    db.add(Incident(project_id=project.id, kind="down", level="broken", started_at=start,
                    ended_at=start + dt.timedelta(minutes=7), title="Your site is not loading", what="w", who="w"))
    db.commit()
    client.post("/api/stripe", json=invoice(created=monday - dt.timedelta(days=1)), headers=key)

    assert run(lambda s: send_weekly(s, SETTINGS, now=monday - dt.timedelta(hours=2)), db) == 0  # before 08:00
    assert run(lambda s: send_weekly(s, SETTINGS, now=monday), db) == 1
    assert run(lambda s: send_weekly(s, SETTINGS, now=monday + dt.timedelta(hours=1)), db) == 0  # only once

    email = emails(db)[0]
    assert email.sender_name == "Prodbat weekly"
    assert email.subject == "yourapp.com this week: 5 visitors, 1 payment, 1 thing broke"
    assert "  - Your site did not load for 7 minutes." in email.body
    assert "Most visitors came from: Google" in email.body
    assert "If this email ever fails to arrive on a Monday, something is wrong with us." in email.body
    assert "upgrade" not in email.body.lower()  # never an offer


# ---- MCP ----------------------------------------------------------------

def rpc(client, method, params=None, headers=None, id=1):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}},
                       headers=headers or {}).json()


def test_mcp_lists_six_tools_and_runs_them_with_the_projects_key(client, db, project, key):
    assert rpc(client, "initialize")["result"]["serverInfo"]["name"] == "prodbat"
    assert client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    tools = rpc(client, "tools/list")["result"]["tools"]
    assert len(tools) == 14 and all(t["inputSchema"]["type"] == "object" for t in tools)

    status = rpc(client, "tools/call", {"name": "prodbat_status"}, key)["result"]
    assert status["content"][0]["text"].startswith("# yourapp.com: Waiting for your first visitor.")
    steps = rpc(client, "tools/call", {"name": "prodbat_suggestion", "arguments": {"id": "S-02"}}, key)["result"]
    assert "Watch a scheduled job" in steps["content"][0]["text"]
    assert "ok   Your site loads" in rpc(client, "tools/call", {"name": "prodbat_verify"}, key)["result"]["content"][0]["text"]
    rpc(client, "tools/call", {"name": "prodbat_test_alert"}, key)
    assert len(emails(db)) == 1

    # Without the key the tool answers with an error the agent can read.
    refused = rpc(client, "tools/call", {"name": "prodbat_status"})["result"]
    assert refused["isError"] is True and "Authorization: Bearer" in refused["content"][0]["text"]
    assert rpc(client, "tools/call", {"name": "nope"}, key)["error"]["code"] == -32602
    assert rpc(client, "resources/list")["error"]["code"] == -32601


def test_mcp_speaks_what_claude_code_and_cursor_send_over_http(client, project, key):
    # The version is the client's when this server speaks it, otherwise its own.
    assert rpc(client, "initialize", {"protocolVersion": "2025-11-25"})["result"]["protocolVersion"] == "2025-11-25"
    assert rpc(client, "initialize", {"protocolVersion": "2099-01-01"})["result"]["protocolVersion"] == "2025-06-18"
    assert rpc(client, "ping") == {"jsonrpc": "2.0", "id": 1, "result": {}}

    # Notifications and a client's answers get 202 and no body.
    for message in ({"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 7, "result": {}}):
        r = client.post("/mcp", json=message)
        assert r.status_code == 202 and r.content == b""
    # No stream to listen to, and no session to end.
    assert client.get("/mcp").status_code == 405
    assert client.delete("/mcp").status_code == 405

    # The agent's config named ${PRODBAT_KEY} but it wasn't set: say how to fix that.
    for sent in ("Bearer ${PRODBAT_KEY}", "Bearer ${env:PRODBAT_KEY}", "Bearer "):
        r = rpc(client, "tools/call", {"name": "prodbat_status"}, {"authorization": sent})["result"]
        assert r["isError"] is True and "PRODBAT_KEY is not set in the environment" in r["content"][0]["text"]
    assert "isError" not in rpc(client, "tools/call", {"name": "prodbat_status"}, key)["result"]


# ---- the worker ---------------------------------------------------------

def test_old_data_is_pruned_and_recent_data_is_kept(db, project):
    from prodbat_server.worker import prune

    db.add_all([view(project, "old", ts=days_ago(400)), view(project, "new", ts=days_ago(2))])
    run(lambda sessions: prune(sessions, SETTINGS), db)
    from prodbat_server.models import Pageview
    assert [v.visitor for v in db.scalars(select(Pageview))] == ["new"]
