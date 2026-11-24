import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import prodbat
import prodbat.django


@pytest.fixture
def server():
    """A stand-in for the Prodbat server that records what it is sent."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def record(self):
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            if self.path != "/api/runtime":  # sent once per process; checked in its own test
                seen.append({"path": self.path, "auth": self.headers["authorization"],
                             "body": json.loads(body) if body else None})
            else:
                runtime.append(json.loads(body))
            self.send_response(202)
            self.end_headers()

        do_POST = do_PUT = record

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    prodbat.init(key="pbs_test", url=f"http://127.0.0.1:{httpd.server_port}/")
    yield seen
    prodbat.flush()
    httpd.shutdown()
    prodbat.init()


runtime = []


def create_session(cart):
    return cart["price_id"]


def test_an_exception_is_reported_with_where_it_happened(server):
    try:
        create_session({})
    except KeyError:
        prodbat.capture_exception(route="POST /api/checkout")
    prodbat.flush()
    report, = server
    assert report["path"] == "/api/errors" and report["auth"] == "Bearer pbs_test"
    body = report["body"]
    assert (body["kind"], body["message"], body["route"], body["status"]) == ("KeyError", "'price_id'", "POST /api/checkout", 500)
    assert body["where"].startswith("tests/test_client.py:") and body["where"].endswith(" in create_session")
    assert "Traceback (most recent call last)" in body["trace"]
    # The first thing sent in a process is which Python it runs on.
    assert runtime[0]["language"] == "python" and runtime[0]["version"].count(".") == 2


def test_a_job_checks_in_and_a_stripe_event_is_forwarded(server):
    prodbat.ping("nightly-email", every="24h")
    prodbat.stripe_event({"type": "invoice.paid", "data": {"object": {"id": "in_1"}}})
    prodbat.flush()
    assert [r["path"] for r in server] == ["/api/ping/nightly-email?every=24h", "/api/stripe"]
    assert server[1]["body"]["type"] == "invoice.paid"


def test_log_lines_go_out_together_with_their_fields(server):
    prodbat.log("refund issued", order=1042, amount=19)
    prodbat.log("card declined", level="warning")
    logger = logging.getLogger("billing")
    logger.addHandler(prodbat.LogHandler())
    logger.setLevel(logging.INFO)
    try:
        raise ValueError("bad total")
    except ValueError:
        logger.exception("could not total the cart %s", 7)
    prodbat.flush()
    lines = [line for r in server if r["path"] == "/api/logs" for line in r["body"]["lines"]]
    assert [(line["message"], line["level"]) for line in lines] == [
        ("refund issued", "info"), ("card declined", "warning"), ("could not total the cart 7", "error")]
    assert lines[0]["fields"] == {"order": 1042, "amount": 19} and lines[1]["fields"] is None
    assert lines[2]["fields"]["logger"] == "billing" and "ValueError: bad total" in lines[2]["fields"]["exception"]
    assert len([r for r in server if r["path"] == "/api/logs"]) <= 2  # batched, not one request per line


def test_a_queue_snapshot_is_reported(server):
    prodbat.queues.report("emails", waiting=3, oldest_seconds=12.5, done=40, failed=1, system="sidekiq")
    prodbat.flush()
    assert server[0]["path"] == "/api/queues"
    assert server[0]["body"] == {"system": "sidekiq", "queues": [{"name": "emails", "waiting": 3, "oldest_seconds": 12.5,
                                                               "done": 40, "failed": 1, "max_wait_seconds": None}]}


def test_the_asgi_middleware_reports_exceptions_and_other_5xx_answers(server):
    from fastapi import FastAPI, Response
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.add_middleware(prodbat.ASGIMiddleware)

    @app.post("/api/checkout")
    def checkout():
        return create_session({})

    @app.get("/api/upstream")
    def upstream():
        return Response(status_code=502)

    @app.get("/api/fine")
    def fine():
        return {}

    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/api/checkout").status_code == 500
    assert client.get("/api/upstream").status_code == 502
    assert client.get("/api/fine").status_code == 200
    assert client.get("/api/missing").status_code == 404
    prodbat.flush()
    assert [(r["body"]["route"], r["body"]["kind"]) for r in server] == [
        ("POST /api/checkout", "KeyError"), ("GET /api/upstream", "HTTP 502")]  # once each, and nothing for 200 or 404
    assert server[0]["body"]["where"].endswith(" in create_session")


def test_the_wsgi_and_django_middleware_report_too(server):
    def raising(environ, start_response):
        raise RuntimeError("boom")

    def unavailable(environ, start_response):
        start_response("503 Service Unavailable", [])
        return [b""]

    with pytest.raises(RuntimeError):
        list(prodbat.WSGIMiddleware(raising)({"REQUEST_METHOD": "GET", "PATH_INFO": "/x"}, None))
    list(prodbat.WSGIMiddleware(unavailable)({"REQUEST_METHOD": "GET", "PATH_INFO": "/y"}, lambda status, headers: None))

    class Request:
        method, path = "POST", "/pay"

    class Answer:
        def __init__(self, status):
            self.status_code = status

    # Django turns an exception into a 500 answer itself: it must be reported once, with its cause.
    request = Request()

    def view(request):
        try:
            raise ValueError("bad")
        except ValueError as exc:
            assert middleware.process_exception(request, exc) is None
        return Answer(500)

    middleware = prodbat.django.Middleware(view)
    assert middleware(request).status_code == 500

    # Http404 and the like become a 404 page: the site working normally, not reported.
    class Http404(Exception):
        pass

    def not_found(request):
        try:
            raise Http404("No product matches the given query.")
        except Http404 as exc:
            not_found_middleware.process_exception(request, exc)
        return Answer(404)

    not_found_middleware = prodbat.django.Middleware(not_found)
    assert not_found_middleware(Request()).status_code == 404
    assert prodbat.django.Middleware(lambda request: Answer(504))(Request()).status_code == 504
    assert prodbat.django.Middleware(lambda request: Answer(200))(Request()).status_code == 200
    prodbat.flush()
    assert [(r["body"]["route"], r["body"]["kind"]) for r in server] == [
        ("GET /x", "RuntimeError"), ("GET /y", "HTTP 503"), ("POST /pay", "ValueError"), ("POST /pay", "HTTP 504")]


def test_rq_queues_are_read_through_the_same_calls_rq_offers(server):
    import datetime

    class Registry:
        def __init__(self, count):
            self.count = count

    class Job:
        enqueued_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=90)

    class Queue:
        name, count = "emails", 4
        finished_job_registry, failed_job_registry = Registry(10), Registry(2)

        def get_job_ids(self, offset, length):
            return ["a"]

        def fetch_job(self, job_id):
            return Job()

    last = {}
    first = prodbat.queues.rq_snapshot(Queue(), last)
    assert (first["waiting"], first["done"], first["failed"]) == (4, 0, 0) and 89 <= first["oldest_seconds"] <= 92
    Queue.finished_job_registry, Queue.failed_job_registry, Queue.count = Registry(25), Registry(3), 0
    assert prodbat.queues.rq_snapshot(Queue(), last) == {"name": "emails", "waiting": 0, "oldest_seconds": 0, "done": 15, "failed": 1}


def test_nothing_is_raised_when_prodbat_cannot_be_reached_or_is_not_configured():
    prodbat.init(key="pbs_test", url="http://127.0.0.1:1")  # nothing listens here
    prodbat.ping("job")
    prodbat.capture_exception(ValueError("x"))
    prodbat.log("hello")
    prodbat.flush()
    prodbat.init(key=None, url=None)
    prodbat.ping("job")
    prodbat.stripe_event(object())
    prodbat.log("hello")
    prodbat.flush()
