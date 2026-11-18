"""Prodbat for your server.

    import prodbat
    app.add_middleware(prodbat.ASGIMiddleware)     # FastAPI, Starlette
    prodbat.flask(app)                             # Flask
    # Django: add "prodbat.django.Middleware" to MIDDLEWARE

    prodbat.log("refund issued", order=1042)       # a log line
    logging.getLogger().addHandler(prodbat.LogHandler())   # or send what you already log

    prodbat.ping("nightly-email", every="24h")     # at the end of a scheduled job
    prodbat.stripe_event(event)                    # in the Stripe webhook handler
    prodbat.queues.watch_celery(celery_app)        # job queues: see prodbat.queues

Set PRODBAT_KEY and PRODBAT_URL in the environment. Nothing here ever
raises or blocks your code: everything goes out on a background thread
and is dropped if Prodbat cannot be reached. No dependencies.
"""

import atexit
import json
import logging
import os
import platform
import queue
import sys
import sysconfig
import threading
import time
import traceback
import urllib.request

__all__ = ["init", "capture_exception", "capture_status", "log", "LogHandler", "ping", "stripe_event", "flush",
           "ASGIMiddleware", "WSGIMiddleware", "flask", "queues"]
__version__ = "0.0.1"

_config = {"key": None, "url": None}
_queue: "queue.Queue[tuple[str, str, dict | None]]" = queue.Queue(maxsize=200)
_logs: list = []
_lock = threading.Lock()
_thread = None
_busy = False
_runtime_sent = False
_LIBRARY_DIRS = tuple({sysconfig.get_paths()[name] for name in ("stdlib", "purelib", "platlib")})
MAX_BUFFERED_LOGS = 2000


def init(key=None, url=None):
    """Optional. Without it, PRODBAT_KEY and PRODBAT_URL are read from the environment."""
    _config["key"], _config["url"] = key, url


def _settings():
    key = _config["key"] or os.environ.get("PRODBAT_KEY")
    url = (_config["url"] or os.environ.get("PRODBAT_URL") or "").rstrip("/")
    return key, url or None


def _deliver(method, path, payload):
    try:
        key, url = _settings()
        if key and url:
            request = urllib.request.Request(
                url + path, method=method,
                data=json.dumps(payload, default=str).encode() if payload is not None else None,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                         "User-Agent": f"prodbat-python/{__version__}"})
            urllib.request.urlopen(request, timeout=5).close()
    except Exception:
        pass  # reporting must never become the problem


def _work():
    global _busy
    while True:
        try:
            item = _queue.get(timeout=1.0)
        except queue.Empty:
            item = None
        _busy = True
        try:
            if item is not None:
                _deliver(*item)
                _queue.task_done()
            while _logs:  # log lines go out together, about once a second
                with _lock:
                    batch, _logs[:] = _logs[:500], _logs[500:]
                _deliver("POST", "/api/logs", {"lines": batch})
        finally:
            _busy = False


def _start():
    global _thread, _runtime_sent
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_work, name="prodbat", daemon=True)
            _thread.start()
        first, _runtime_sent = not _runtime_sent, True
    if first:  # tell Prodbat which Python this really runs on, once per process
        try:
            _queue.put_nowait(("PUT", "/api/runtime", {"language": "python", "version": platform.python_version()}))
        except Exception:
            pass


def _send(method, path, payload=None):
    try:
        _start()
        _queue.put_nowait((method, path, payload))
    except Exception:
        pass  # a full queue drops the report


def _after_fork():
    # A forked worker (gunicorn, Celery) must not share the parent's queue or thread.
    global _queue, _lock, _thread, _logs, _runtime_sent
    _queue, _lock, _thread, _logs, _runtime_sent = queue.Queue(maxsize=200), threading.Lock(), None, [], True


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def flush(timeout=2.0):
    """Wait for everything queued to go out. Call it before a serverless
    function returns; it is also called when the process exits."""
    deadline = time.monotonic() + timeout
    while (_queue.unfinished_tasks or _logs or _busy) and time.monotonic() < deadline:
        time.sleep(0.02)


atexit.register(flush)


# ---- errors ---------------------------------------------------------------

def _where(tb):
    """The deepest frame in the app's own code: 'billing/views.py:88 in create_session'."""
    frames = traceback.extract_tb(tb)
    own = [f for f in frames if not f.filename.startswith(_LIBRARY_DIRS) and "site-packages" not in f.filename
           and not f.filename.startswith("<")]
    if not (own or frames):
        return ""
    frame = (own or frames)[-1]
    short = "/".join(frame.filename.replace("\\", "/").split("/")[-2:])
    return f"{short}:{frame.lineno} in {frame.name}"


def capture_exception(exc=None, route=""):
    """Report an exception. With no argument, reports the one being handled."""
    try:
        exc = exc or sys.exc_info()[1]
        if exc is None:
            return
        _send("POST", "/api/errors", {
            "kind": type(exc).__name__[:200],
            "message": str(exc)[:1000],
            "route": route[:300],
            "where": _where(exc.__traceback__)[:300],
            "trace": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-20000:],
            "status": 500,
        })
    except Exception:
        pass


def capture_status(status, route=""):
    """Report a 5xx answer that did not come from an exception."""
    if status >= 500:
        _send("POST", "/api/errors", {"kind": f"HTTP {status}", "status": int(status), "route": route[:300]})


# ---- logs -----------------------------------------------------------------

def log(message, level="info", **fields):
    """Send a log line. Extra keyword arguments are stored with it:
    prodbat.log("refund issued", order=1042, amount=19)"""
    try:
        with _lock:
            if len(_logs) < MAX_BUFFERED_LOGS:
                _logs.append({"message": str(message)[:4000], "level": str(level).lower(), "t": time.time(),
                              "fields": fields or None})
        _start()
    except Exception:
        pass


class LogHandler(logging.Handler):
    """Send what the app already logs:
    logging.getLogger().addHandler(prodbat.LogHandler(level=logging.INFO))"""

    def emit(self, record):
        try:
            if record.name.startswith("prodbat"):
                return
            fields = {"logger": record.name}
            if record.exc_info:
                fields["exception"] = "".join(traceback.format_exception(*record.exc_info))[-4000:]
            log(record.getMessage(), record.levelname, **fields)
        except Exception:
            pass


# ---- jobs and payments ----------------------------------------------------

def ping(name, every=None):
    """A scheduled job checks in. Call it at the end, after the work has
    succeeded. `every` says how often the job runs: "15m", "24h", "7d"."""
    _send("POST", f"/api/ping/{name}" + (f"?every={every}" if every else ""))


def stripe_event(event):
    """Forward a Stripe webhook event, after its signature has been checked."""
    try:
        if not isinstance(event, dict):
            event = event.to_dict() if hasattr(event, "to_dict") else json.loads(str(event))
        _send("POST", "/api/stripe", event)
    except Exception:
        pass


# ---- web frameworks -------------------------------------------------------

class ASGIMiddleware:
    """For FastAPI, Starlette and other ASGI apps. Reports unhandled
    exceptions with their cause, and any other 5xx answer."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        route = f"{scope.get('method', '')} {scope.get('path', '')}".strip()
        seen = {}

        async def watch(message):
            if message.get("type") == "http.response.start":
                seen["status"] = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, watch)
        except Exception as exc:
            capture_exception(exc, route)
            raise
        capture_status(seen.get("status", 0), route)


class WSGIMiddleware:
    """For WSGI apps. Flask handles its own exceptions; use prodbat.flask(app) there."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        route = f"{environ.get('REQUEST_METHOD', '')} {environ.get('PATH_INFO', '')}".strip()
        seen = {}

        def watch(status, headers, exc_info=None):
            seen["status"] = int(str(status)[:3] or 0)
            return start_response(status, headers, exc_info) if exc_info else start_response(status, headers)

        try:
            yield from self.app(environ, watch)
        except Exception as exc:
            capture_exception(exc, route)
            raise
        capture_status(seen.get("status", 0), route)


def flask(app):
    """Report unhandled exceptions and other 5xx answers in a Flask app."""
    from flask import g, got_request_exception, request

    def failed(sender, exception, **extra):
        g._prodbat_reported = True
        capture_exception(exception, f"{request.method} {request.path}")

    def answered(response):
        if response.status_code >= 500 and not getattr(g, "_prodbat_reported", False):
            capture_status(response.status_code, f"{request.method} {request.path}")
        return response

    got_request_exception.connect(failed, app, weak=False)
    app.after_request(answered)


from . import queues  # noqa: E402  (uses _send)
