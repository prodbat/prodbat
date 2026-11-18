import json

from sqlalchemy import func, select

from prodbat_server.models import BrowserError, DailySalt, FailedRequest, Pageview


def post(client, event, ip="203.0.113.7", **headers):
    # Sent the way sendBeacon sends it: a JSON string as text/plain.
    return client.post(
        "/api/e",
        content=json.dumps({"k": "pb_test", **event}),
        headers={"content-type": "text/plain;charset=UTF-8", "x-forwarded-for": ip, **headers},
    )


def test_pageview_is_stored_with_its_source(client, db, project):
    r = post(client, {"t": "pv", "u": "https://www.yourapp.com/pricing?plan=pro", "r": "https://chatgpt.com/"})
    assert r.status_code == 202
    pv = db.scalars(select(Pageview)).one()
    assert (pv.path, pv.source, pv.source_kind) == ("/pricing", "ChatGPT", "ai")
    assert len(pv.visitor) == 32


def test_same_browser_is_one_visitor_and_another_address_is_a_second(client, db, project):
    post(client, {"t": "pv", "u": "https://yourapp.com/"})
    post(client, {"t": "pv", "u": "https://yourapp.com/pricing", "r": "https://yourapp.com/"})
    post(client, {"t": "pv", "u": "https://yourapp.com/"}, ip="198.51.100.9")
    assert db.scalar(select(func.count(func.distinct(Pageview.visitor)))) == 2
    assert db.scalar(select(func.count()).select_from(Pageview)) == 3


def test_client_address_is_taken_from_the_trusted_end_of_forwarded_for(client, db, project):
    # proxy_hops=1: the right-most entry is what our proxy saw. A spoofed
    # left-most entry must not create a new visitor.
    post(client, {"t": "pv", "u": "https://yourapp.com/"}, ip="203.0.113.7")
    post(client, {"t": "pv", "u": "https://yourapp.com/a"}, ip="1.2.3.4, 203.0.113.7")
    assert db.scalar(select(func.count(func.distinct(Pageview.visitor)))) == 1


def test_neither_address_nor_user_agent_is_stored(client, db, project):
    post(client, {"t": "pv", "u": "https://yourapp.com/"})
    pv = db.scalars(select(Pageview)).one()
    assert "203.0.113.7" not in pv.visitor
    assert db.scalar(select(func.count()).select_from(DailySalt)) == 1


def test_browser_error_is_stored_and_fingerprinted(client, db, project):
    event = {"t": "err", "u": "https://yourapp.com/signup", "m": "x is not defined", "f": "https://yourapp.com/app.js", "l": 12, "c": 4, "s": "ReferenceError: x is not defined\n at app.js:12:4"}
    assert post(client, event).status_code == 202
    assert post(client, {**event, "c": 9}).status_code == 202
    errors = db.scalars(select(BrowserError)).all()
    assert len(errors) == 2
    assert errors[0].fingerprint == errors[1].fingerprint
    assert errors[0].path == "/signup" and errors[0].line == 12


def test_failed_request_is_stored_without_its_query_string(client, db, project):
    r = post(client, {"t": "req", "u": "https://yourapp.com/checkout", "me": "post", "ru": "/api/checkout?token=secret", "st": 500})
    assert r.status_code == 202
    fr = db.scalars(select(FailedRequest)).one()
    assert (fr.method, fr.route, fr.status, fr.path) == ("POST", "/api/checkout", 500, "/checkout")


def test_a_request_that_did_not_fail_is_refused(client, db, project):
    r = post(client, {"t": "req", "u": "https://yourapp.com/", "ru": "/api/ok", "st": 404})
    assert r.status_code == 400
    assert db.scalar(select(func.count()).select_from(FailedRequest)) == 0


def test_unknown_key_and_wrong_domain_are_refused(client, db, project):
    assert post(client, {"k": "pb_nope", "t": "pv", "u": "https://yourapp.com/"}).status_code == 404
    assert post(client, {"t": "pv", "u": "https://evil.example/"}).status_code == 400
    assert db.scalar(select(func.count()).select_from(Pageview)) == 0


def test_bots_are_accepted_and_dropped(client, db, project):
    r = post(client, {"t": "pv", "u": "https://yourapp.com/"}, **{"user-agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"})
    assert r.status_code == 202
    assert db.scalar(select(func.count()).select_from(Pageview)) == 0


def test_garbage_and_oversized_bodies_are_refused(client, project):
    assert client.post("/api/e", content="hello").status_code == 400
    assert client.post("/api/e", content="x" * 9000).status_code == 413


def test_script_is_served_and_small(client):
    r = client.get("/p.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert len(r.content) <= 4096
