# Prodbat

Simple analytics and monitoring for solo founders who build with AI coding tools. One small script and one page: visitors, where they came from, what people pay for, what is breaking, and an email when something breaks.

This repository is the open-source product. It is early: it runs and is tested locally, and has not yet been run on a real site.

## What it does

- **Visitors and sources**, counted without cookies. AI assistants (ChatGPT, Claude, Perplexity and others) are named.
- **What sells**, by product, from Stripe events your own webhook handler forwards.
- **What is breaking**: errors in the page, requests that fail, and every 5xx your server returns, with the file and line.
- **Paying and signing in come first**: a failure in checkout, sign-in or sign-up alerts at once and says "People cannot pay".
- **Outside checks**: your home page, sign-in and pricing pages are loaded every five minutes; certificate and domain expiry daily.
- **Versions**: for the language, the framework and every package, including ones pulled in by others: known security problems (OSV), versions deprecated by their authors (deps.dev), and release lines that no longer get security fixes (endoflife.date). Reads npm, Python, Ruby, PHP, Go and Rust lockfiles.
- **Background work**: scheduled jobs that stop running, and job queues (Celery, RQ, BullMQ, or any other over HTTP) that are stuck, silent or failing.
- **Logs**: send log lines from your app, search them, and see what was logged around each problem.
- **Email**: "Broken:" at once, "Needs a look:", "Working again:" in the same thread, and a short report every Monday.
- **Slack, Telegram, phone and texts**: the same alerts in a Slack channel or a Telegram chat, one message per problem with "Working again" replying to it; as notifications on your phone or computer, with no app to install (on an iPhone, add the page to the Home Screen first); a text message when customers are affected.
- **For coding agents**: a command line (`npx prodbat`), an MCP endpoint, a skill, and `llms.txt`.

## Layout

| Path | What it is | Licence |
|---|---|---|
| `server/` | The server: FastAPI and Postgres. See `server/README.md` | AGPL-3.0 |
| `packages/js/` | `npx prodbat` (check, init, verify, status, logs, suggestions, deps) and the library for Node, Deno, Supabase Edge Functions and Cloudflare Workers | MIT |
| `packages/python/` | `pip install prodbat`: the library for Python servers | MIT |
| `packages/ruby/` | `gem "prodbat"`: Rails (automatic), any Rack app, Sidekiq | MIT |
| `packages/php/` | `composer require prodbat/prodbat`: Laravel (automatic) and plain PHP | MIT |
| `docs/http-api.md` | Reporting from any other language with plain HTTP | |
| `packages/skill/` | A skill that teaches a coding agent when and how to use the command line | MIT |
| `docs/self-hosting.md` | Running it yourself | |
| `compose.yaml`, `.env.example` | One container and Postgres | |
| `server/render.yaml` | One-click deploy on Render: the server, its worker and Postgres (Blueprint path `server/render.yaml`) | |

## Try it locally

```sh
cd server
createdb prodbat_dev
uv sync
PRODBAT_ADMIN_TOKEN=dev uv run python -m prodbat_server run
```

Then, in a project you want to watch:

```sh
node /path/to/prodbat/packages/js/bin/prodbat.mjs init --server http://localhost:8000 --token dev --email you@example.com
```

## Tests

```sh
cd server && createdb prodbat_test && uv run pytest
cd packages/js && node --test
cd packages/python && PYTHONPATH=. uv run --project ../../server pytest
```
