"""Extra things Prodbat could watch, as instructions for a coding agent.

The text is fixed and versioned here. Only two values are filled in, the
server address and the site's domain, and both come from this server's
own records. Nothing a visitor, an email or a log line says ever ends up
in these instructions.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Project

VERIFY_FIRST = ("Before changing anything, read the relevant code and confirm it matches what is described "
                "here. If it does not, stop and tell the user what you found instead.")

SUGGESTIONS = [
    {
        "id": "S-01",
        "title": "Count payments by product",
        "pitch": "We could also show what people pay for, from Stripe.",
        "applies": "select not exists (select 1 from payments where project_id = :p) "
                   "and (select stripe_seen_at from projects where id = :p) is null",
        "steps": """\
This project takes payments with Stripe and already has a webhook handler. Prodbat can show what sells
if that handler forwards each event it receives.

1. Find the Stripe webhook handler (search for `constructEvent`, `construct_event` or `stripe-signature`).
2. After the signature has been checked and before the handler returns, forward the event. Do not
   await it in a way that can fail the handler; a Prodbat outage must never break payments.

   Python:
       import prodbat            # pip install prodbat
       prodbat.stripe_event(event)

   JavaScript / TypeScript:
       fetch("{server}/api/stripe", {{
         method: "POST",
         headers: {{ authorization: `Bearer ${{process.env.PRODBAT_KEY}}`, "content-type": "application/json" }},
         body: JSON.stringify(event),
       }}).catch(() => {{}});

3. Make sure PRODBAT_KEY (and for Python, PRODBAT_URL={server}) is set where the app is hosted.
4. To see which source each buyer came from, add `client_reference_id: window.prodbat.ref` where a
   Checkout Session is created from the browser. Stripe Payment Links are handled automatically.
5. Run `npx prodbat verify`.""",
    },
    {
        "id": "S-02",
        "title": "Watch a scheduled job",
        "pitch": "We could also tell you when a scheduled job (a nightly email, a backup) stops running.",
        "applies": "select not exists (select 1 from jobs where project_id = :p)",
        "steps": """\
Prodbat can tell the owner when a scheduled job stops running. The job checks in when it finishes;
if it goes one and a half times its usual gap without checking in, the owner is told.

1. Find the project's scheduled jobs (vercel.json `crons`, render.yaml cron services, Celery beat,
   GitHub Actions `schedule`, a crontab). List them for the user and ask which ones matter.
2. For each one, at the very end of the job, after the work has succeeded, add one request. Pick a
   short name and say how often the job runs (m, h or d):

   Shell:
       curl -fsS -m 10 -X POST "{server}/api/ping/nightly-email?every=24h" -H "Authorization: Bearer $PRODBAT_KEY"

   Python:
       import prodbat
       prodbat.ping("nightly-email", every="24h")

   JavaScript / TypeScript:
       await fetch("{server}/api/ping/nightly-email?every=24h", {{
         method: "POST", headers: {{ authorization: `Bearer ${{process.env.PRODBAT_KEY}}` }},
       }}).catch(() => {{}});

3. Make sure PRODBAT_KEY is set wherever the job runs.
4. Run the job once by hand, then `npx prodbat status` to confirm it checked in.""",
    },
    {
        "id": "S-03",
        "title": "Report every server error with its cause",
        "pitch": "We could also catch every 500 your server returns, with the file and line that caused it.",
        # Done once the server has reported in: the libraries send the language version
        # at start-up, so a healthy server counts without waiting for its first error.
        "applies": "select not exists (select 1 from server_errors where project_id = :p) "
                   "and not exists (select 1 from projects where id = :p and first_server_at is not null)",
        "steps": """\
The browser script only sees failures a visitor's browser hits. A few lines in the server report every
unhandled error with its file and line, and every other 5xx answer, including webhooks and jobs.

JavaScript / TypeScript (run `npm install prodbat`):
    const prodbat = require("prodbat");            // or: import * as prodbat from "prodbat"
    // Express
    app.use(prodbat.express());                    // before the routes
    app.use(prodbat.expressErrorHandler());        // after the routes
    // Next.js 15 or later, in instrumentation.ts
    export {{ onRequestError }} from "prodbat";
    // Anything that takes a Request and returns a Response (Next.js route handlers, Hono)
    export const POST = prodbat.wrap(handler);

Supabase Edge Functions and Deno (set PRODBAT_KEY and PRODBAT_URL with `supabase secrets set`):
    import * as prodbat from "npm:prodbat";
    Deno.serve(prodbat.wrap(async (req) => {{ /* the existing handler */ }}));

Cloudflare Workers (PRODBAT_KEY as a secret, PRODBAT_URL under [vars]):
    import * as prodbat from "prodbat";
    export default {{ fetch: prodbat.wrap(async (request, env, ctx) => {{ /* the existing handler */ }}) }};

Python (run `pip install prodbat`):
    import prodbat
    app.add_middleware(prodbat.ASGIMiddleware)     # FastAPI, Starlette
    prodbat.flask(app)                             # Flask
    # Django: add "prodbat.django.Middleware" to MIDDLEWARE

Ruby (add `gem "prodbat"` to the Gemfile, then bundle install):
    # Rails: nothing more; the gem adds its middleware itself.
    use Prodbat::Middleware                        # Sinatra, Hanami, any Rack app, in config.ru
    Prodbat::Sidekiq.watch                         # Sidekiq queues, after require "prodbat/sidekiq"

PHP (run `composer require prodbat/prodbat`):
    // Laravel: nothing more; the package registers itself.
    \\Prodbat\\Prodbat::register();                 // any other PHP app, right after the autoloader

Any other language: where the app handles an unhandled error or returns a 5xx, send one request and
ignore its result. Only `kind` is required.
    POST {server}/api/errors
    Authorization: Bearer $PRODBAT_KEY
    {{"kind": "RuntimeError", "message": "...", "route": "POST /api/checkout",
      "where": "app/billing.rb:88 in create_session", "trace": "...", "status": 500}}

PRODBAT_KEY and PRODBAT_URL={server} need to be set where the app is hosted.""",
        "after": "Deploy, then run `npx prodbat verify`.",
    },
    {
        "id": "S-04",
        "title": "Serve the script from your own domain",
        "pitch": "Some visitors block scripts from other domains. Serving ours from yours counts them too.",
        "applies": "select cast(:p as text) is not null",
        "steps": """\
Ad blockers stop some visitors from loading scripts from another domain. Serving the Prodbat script
and its endpoint through {domain} itself avoids that.

1. Add two rewrites to the host's configuration so that:
       /pb/p.js   ->  {server}/p.js
       /pb/api/e  ->  {server}/api/e
   Next.js: `rewrites()` in next.config. Vercel: `rewrites` in vercel.json. Netlify: `_redirects`
   with status 200. Nginx or Caddy: a reverse proxy location.
2. Change the script tag to `src="/pb/p.js"` and add `data-api="/pb/api/e"`. Keep `data-site`.
3. The proxy must pass the visitor's address on in X-Forwarded-For, or every visitor will look the same.
4. Deploy, then run `npx prodbat verify`.""",
    },
    {
        "id": "S-05",
        "title": "Security and end-of-life alerts for everything the app is built on",
        "pitch": "We could also tell you when the language, framework or a package your app uses has a "
                 "security problem or is no longer supported.",
        "applies": "select not exists (select 1 from dependencies where project_id = :p)",
        "steps": """\
Prodbat checks the language, the framework and every package the app uses, including the ones pulled
in by other packages: known security problems (from OSV), versions their authors have deprecated (from
deps.dev), and release lines that no longer get security fixes (from endoflife.date). Serious, fixable
problems are emailed; the rest are listed in `npx prodbat status`. It needs names and versions only.
No source code is sent.

1. Run `npx prodbat deps` in the project root. It reads package-lock.json, pnpm-lock.yaml, yarn.lock,
   uv.lock, poetry.lock, requirements.txt, Gemfile.lock, composer.lock, go.mod and Cargo.lock, and the
   files that pin the language version (.nvmrc, .python-version, .ruby-version, go.mod).
2. Add the same command to the deploy or build step so the list stays current.
3. The language version is most reliable when the running server reports it: the prodbat libraries for
   JavaScript and Python do that by themselves. From another language, send it once at start-up:
       PUT {server}/api/runtime   {{"language": "ruby", "version": "3.3.5"}}""",
    },
    {
        "id": "S-06",
        "title": "Watch the job queue",
        "pitch": "We could also tell you when background jobs pile up, stop, or start failing.",
        "applies": "select not exists (select 1 from queue_reports where project_id = :p)",
        "steps": """\
If this project runs background jobs through a queue, Prodbat can tell the owner when jobs are stuck
(the oldest has waited 15 minutes, or nothing is finishing), when the queue goes quiet, and when many
jobs fail. First find which queue system the project uses; if it has none, tell the user and stop.

Celery (Python), in the module that defines the Celery app, so web and workers both run it:
    import prodbat
    prodbat.queues.watch_celery(app)

RQ (Python), in the web process or a worker:
    import prodbat
    prodbat.queues.watch_rq([queue])               # the rq.Queue objects

BullMQ (Node), in any long-lived process that has the Queue objects:
    const prodbat = require("prodbat");
    prodbat.watchBullMQ([queue]);

Anything else (Sidekiq, Laravel queues, Oban, pg-boss, Solid Queue, a jobs table): once a minute, send
what you can measure. Leave out what you cannot.
    POST {server}/api/queues
    Authorization: Bearer $PRODBAT_KEY
    {{"system": "sidekiq", "queues": [{{"name": "default", "waiting": 12, "oldest_seconds": 40,
      "done": 310, "failed": 2}}]}}
  `waiting` is how many jobs have not started, `oldest_seconds` how long the oldest of them has waited,
  `done` and `failed` are counts since the previous report.
  Sidekiq: Sidekiq::Queue.new("default").size and .latency give the first two.

Make sure PRODBAT_KEY and PRODBAT_URL={server} are set wherever this runs.""",
        "after": "Deploy, wait two minutes, then run `npx prodbat status`: the queue is listed with what is waiting.",
    },
    {
        "id": "S-07",
        "title": "Send logs",
        "pitch": "We could also keep your app's logs, and show what it logged around each problem.",
        "applies": "select not exists (select 1 from log_lines where project_id = :p)",
        "steps": """\
The app can send log lines to Prodbat. The owner can read and search them, and the warnings and errors
logged around a problem are included in `npx prodbat status`. Ask the user what they want logged; a
good default is warnings and errors, plus a line at each step of paying and signing in.

Python:
    import logging, prodbat
    logging.getLogger().addHandler(prodbat.LogHandler(level=logging.WARNING))   # what is already logged
    prodbat.log("refund issued", order=1042)                                    # or a line of your own

JavaScript / TypeScript:
    const prodbat = require("prodbat");
    prodbat.log("refund issued", {{ order: 1042 }});
    prodbat.log.warn("card declined");  prodbat.log.error("could not send email", {{ to }});
    // In a serverless function, `await prodbat.flush()` before returning.

Any other language, up to 500 lines per request:
    POST {server}/api/logs
    Authorization: Bearer $PRODBAT_KEY
    {{"lines": [{{"message": "refund issued", "level": "info", "fields": {{"order": 1042}}}}]}}

Never log passwords, card numbers, tokens or full request bodies. Read them back with
`npx prodbat logs`, `npx prodbat logs "refund" --since 2h`, or `npx prodbat logs --level error`.""",
    },
]

BY_ID = {s["id"]: s for s in SUGGESTIONS}


def render(suggestion: dict, project: Project, server: str, in_setup: bool = False) -> dict:
    """The steps for one suggestion. During setup the agent ships every item together at the end, so
    the "deploy, then check" line that ends a suggestion on its own is left out."""
    steps = suggestion["steps"].format(server=server, domain=project.domain)
    if suggestion.get("after") and not in_setup:
        steps += f"\n\n{suggestion['after']}"
    return {"id": suggestion["id"], "title": suggestion["title"],
            "instructions": f"{VERIFY_FIRST}\n\n{steps}"}


async def applicable(db: AsyncSession, project: Project) -> list[dict]:
    return [s for s in SUGGESTIONS if await db.scalar(text(s["applies"]), {"p": project.id})]


async def first_suggestion(db: AsyncSession, project: Project) -> dict | None:
    found = await applicable(db, project)
    return {"id": found[0]["id"], "pitch": found[0]["pitch"]} if found else None
