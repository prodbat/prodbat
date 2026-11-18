# Prodbat server

FastAPI and Postgres, with no queue, cache or other service. One process serves the script, the page and the API; a worker (which can run in the same process) loads each site from outside, decides what counts as a problem, and sends email.

## Run it

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/) and a Postgres database.

```sh
createdb prodbat_dev
uv sync
uv run python -m prodbat_server run                  # migrate, serve on :8000 and watch
uv run python -m prodbat_server add-site yourapp.com --email you@example.com
```

`add-site` prints the script tag, the private link to the page, and the secret key for the site's own server and the command line. Settings are `PRODBAT_*` environment variables; see `../.env.example` and `../docs/self-hosting.md`.

## Test it

```sh
createdb prodbat_test
uv run pytest
```

The tests use a real Postgres database (`PRODBAT_TEST_DATABASE_URL` to change it) and, if `node` is installed, run the browser script against a fake page. Nothing in the tests touches the network.

## How visitors are counted

No cookies and nothing stored in the browser. Each event is tagged with a hash of a random daily salt, the site's domain, the visitor's address and their browser's user-agent. The address and user-agent are never stored, and the salt is deleted when the day ends, so a hash cannot be recomputed or linked across days. A week's visitor count is therefore the sum of each day's.

## Layout

Everything is in `prodbat_server/`.

| File | What it does |
|---|---|
| `static/p.js` | The browser script (kept under 4 KB): page views, errors, failed requests, a reference for Stripe |
| `ingest.py` | `POST /api/e`, where the script posts |
| `sources.py` | Names where a visitor came from |
| `checks.py` | Loads each site from outside; certificate and domain expiry |
| `detect.py` | The rules that turn what was reported into problems. They are first guesses and are all at the top of the file |
| `incidents.py` | Opens and closes problems and writes the emails |
| `mailer.py` | Sends the outbox over SMTP |
| `weekly.py` | The Monday email |
| `stripe_events.py` | Turns forwarded Stripe events into payments |
| `flows.py` | Recognises routes that are part of paying, signing in and signing up |
| `advisories.py` | Security problems in every package, from OSV |
| `freshness.py` | End of life for the language and framework (endoflife.date), Node.js security releases, and deprecated or outdated packages (deps.dev) |
| `suggestions.py` | Fixed instructions for a coding agent to add more things to watch |
| `summary.py` | Gathers everything said about a site, for the page, the weekly email and `status` |
| `page.py`, `templates/page.html` | The page at `/d/<token>`. There is no sign-in; the link is the secret |
| `api.py` | The API for the command line and the site's own server, including job queues and logs |
| `mcp.py` | `POST /mcp`, the same things as tools for a coding agent |
| `worker.py` | Everything on a timer |
| `models.py`, `migrations/` | Tables and their migrations |

## Changing the tables

```sh
uv run alembic revision --autogenerate -m "what changed"
uv run alembic upgrade head
```
