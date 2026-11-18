"""Job queues: how many jobs are waiting, how long the oldest has waited,
and how many are failing. Prodbat tells the owner when a queue is stuck,
has gone quiet, or is failing.

    import prodbat
    prodbat.queues.watch_celery(app)            # Celery: call where the app is defined
    prodbat.queues.watch_rq([queue])            # RQ: call in the web process or a worker
    prodbat.queues.report("emails", waiting=3, oldest_seconds=12)   # anything else, once a minute

Each watcher starts one background thread that reports once a minute.
"""

import datetime
import json
import os
import threading
import time
from collections import defaultdict

from . import _send

__all__ = ["report", "watch_celery", "watch_rq"]


def report(name, waiting=None, oldest_seconds=None, done=0, failed=0, max_wait_seconds=None, system="custom"):
    """One snapshot of one queue. `done` and `failed` are counts since the
    previous report; leave out what you cannot measure."""
    _send("POST", "/api/queues", {"system": system, "queues": [{
        "name": str(name), "waiting": waiting, "oldest_seconds": oldest_seconds, "done": done, "failed": failed,
        "max_wait_seconds": max_wait_seconds}]})


def _every(interval, tick):
    """Run `tick` on a daemon thread every `interval` seconds, restarting
    in a forked child the first time it is asked for there."""
    state = {"pid": None}

    def loop():
        while True:
            time.sleep(interval)
            try:
                tick()
            except Exception:
                pass  # a broker hiccup must not stop the reports

    def ensure():
        if state["pid"] != os.getpid():
            state["pid"] = os.getpid()
            threading.Thread(target=loop, name="prodbat-queues", daemon=True).start()

    return ensure


# ---- RQ --------------------------------------------------------------------

def _age(when):
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (datetime.datetime.now(datetime.timezone.utc) - when).total_seconds())


def rq_snapshot(q, last):
    """What to report for one RQ queue. `last` remembers the registry
    counts from the previous call so only the change is sent."""
    waiting = q.count
    oldest = None
    if waiting:
        ids = q.get_job_ids(0, 1)
        job = q.fetch_job(ids[0]) if ids else None
        oldest = _age(getattr(job, "enqueued_at", None)) if job else None
    counts = {"done": q.finished_job_registry.count, "failed": q.failed_job_registry.count}
    before = last.get(q.name, counts)
    last[q.name] = counts
    return {"name": q.name, "waiting": waiting, "oldest_seconds": oldest if waiting else 0,
            "done": max(0, counts["done"] - before["done"]), "failed": max(0, counts["failed"] - before["failed"])}


def watch_rq(queues, interval=60):
    """Report these RQ queues once a minute. Run it in any long-lived
    process that can reach Redis; the web process is fine."""
    last = {}

    def tick():
        _send("POST", "/api/queues", {"system": "rq", "queues": [rq_snapshot(q, last) for q in queues]})

    _every(interval, tick)()


# ---- Celery ----------------------------------------------------------------

def celery_depth(app, name):
    """Jobs waiting in a queue and how long the oldest has waited. Only
    known for a Redis broker; (None, None) otherwise."""
    try:
        with app.connection_for_read() as connection:
            if connection.transport.driver_type != "redis":
                return None, None
            client = connection.default_channel.client
            waiting = client.llen(name)
            oldest = None
            if waiting:
                raw = client.lindex(name, -1)  # the next message to be taken
                sent = (json.loads(raw).get("headers") or {}).get("prodbat_sent") if raw else None
                oldest = max(0.0, time.time() - float(sent)) if sent else None
            return waiting, oldest
    except Exception:
        return None, None


def watch_celery(app, interval=60):
    """Report this Celery app's queues once a minute. Call it in the
    module that defines the app, so it runs in the web process (which sees
    what is waiting) and in the workers (which see how long jobs waited
    and whether they failed)."""
    from celery import signals

    stats = defaultdict(lambda: {"done": 0, "failed": 0, "max_wait": None})
    names = set()
    lock = threading.Lock()

    def tick():
        with lock:
            seen = {name: stats.pop(name) for name in list(stats)}
        snapshots = []
        for name in sorted(names | set(seen)):
            s = seen.get(name, {"done": 0, "failed": 0, "max_wait": None})
            waiting, oldest = celery_depth(app, name)
            snapshots.append({"name": name, "waiting": waiting, "oldest_seconds": 0 if waiting == 0 else oldest,
                              "done": s["done"], "failed": s["failed"], "max_wait_seconds": s["max_wait"]})
        if snapshots:
            _send("POST", "/api/queues", {"system": "celery", "queues": snapshots})

    ensure = _every(interval, tick)

    def queue_of(task):
        info = getattr(task.request, "delivery_info", None) or {}
        return info.get("routing_key") or getattr(app.conf, "task_default_queue", None) or "celery"

    def published(headers=None, routing_key=None, **extra):
        if headers is not None:
            headers["prodbat_sent"] = time.time()
        names.add(routing_key or getattr(app.conf, "task_default_queue", None) or "celery")
        ensure()

    def started(task=None, **extra):
        name = queue_of(task)
        sent = getattr(task.request, "prodbat_sent", None)
        with lock:
            names.add(name)
            if sent:
                wait = max(0.0, time.time() - float(sent))
                stats[name]["max_wait"] = max(stats[name]["max_wait"] or 0.0, wait)
        ensure()

    def finished(task=None, state=None, **extra):
        with lock:
            stats[queue_of(task)]["failed" if state == "FAILURE" else "done"] += 1

    signals.before_task_publish.connect(published, weak=False)
    signals.task_prerun.connect(started, weak=False)
    signals.task_postrun.connect(finished, weak=False)
    return tick
