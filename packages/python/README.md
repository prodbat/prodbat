# prodbat (Python)

Monitoring for apps built with AI. This package sends a Python app's server errors, log lines, scheduled-job check-ins, job queue health and Stripe events to [Prodbat](https://github.com/prodbat/prodbat). It has no dependencies, never raises, and never holds a request up: reports go out from a background thread and are dropped if Prodbat can't be reached.

## Install

```sh
pip install prodbat
```

Set `PRODBAT_KEY` and `PRODBAT_URL` in the environment (`npx prodbat init` writes them to `.env`).

- **FastAPI, Starlette, any ASGI app**: `app.add_middleware(prodbat.ASGIMiddleware)`
- **Flask**: `prodbat.flask(app)`
- **Django**: add `"prodbat.django.Middleware"` to `MIDDLEWARE`
- **Any WSGI app**: `app = prodbat.WSGIMiddleware(app)`

Each one reports every exception with its traceback and route, and other 5xx answers.

## Also

```python
import logging
import prodbat

prodbat.log("refund issued", order=1042)                 # a log line, with fields
logging.getLogger().addHandler(prodbat.LogHandler())     # or send what the app already logs
prodbat.ping("nightly-email", every="24h")               # at the end of a scheduled job, after it succeeded
prodbat.stripe_event(event)                              # in the Stripe webhook, after the signature check
prodbat.capture_exception(route="POST /x")               # report the current exception yourself

prodbat.queues.watch_celery(celery_app)                  # Celery: where the app is defined
prodbat.queues.watch_rq([queue])                         # RQ
prodbat.queues.report("emails", waiting=3, oldest_seconds=12)   # any other queue, once a minute
```

Each queue watcher reports every minute how many jobs are waiting, how long the oldest has waited, and how many failed. Prodbat tells you when a queue is stuck, has gone quiet, or is failing.

## Tests

```sh
python -m pytest
```

MIT licence.
