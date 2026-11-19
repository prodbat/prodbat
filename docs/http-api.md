# Reporting to Prodbat from any language

The JavaScript and Python libraries are thin wrappers around a few HTTP requests. From Ruby, PHP, Go, Elixir or anything else, send the same requests yourself.

Every request carries the project's secret key and a JSON body:

```
Authorization: Bearer $PRODBAT_KEY
Content-Type: application/json
```

Three rules apply to all of them: send in the background, use a short timeout (5 seconds), and ignore the result. Prodbat being unreachable must never break your app.

## Server errors and 5xx answers

`POST /api/errors`, wherever your app handles an unhandled error or is about to answer 500 or above.

```json
{"kind": "RuntimeError", "message": "price_id missing", "route": "POST /api/checkout",
 "where": "app/billing.rb:88 in create_session", "trace": "...", "status": 500}
```

Only `kind` is required. `where` is the deepest frame in your own code; errors with the same `kind` and `where` are counted as one problem. For a 5xx answer with no exception behind it, send `{"kind": "HTTP 502", "status": 502, "route": "GET /api/upstream"}`.

A route that is part of paying, signing in or signing up alerts on the first error. Routes are recognised by name; others can be listed in `prodbat.json`.

## Logs

`POST /api/logs`, up to 500 lines per request. Batch them: once a second is plenty.

```json
{"lines": [{"message": "refund issued", "level": "info", "t": 1763460000.5, "fields": {"order": 1042}}]}
```

`level` is `debug`, `info`, `warning` or `error`. `t` is seconds since 1970 and may be left out. A message is cut at 4,000 characters. A site may send 3,000 lines a minute by default; the answer says how many were stored and how many dropped.

## Job queues

`POST /api/queues`, once a minute, from any process that can see the queue.

```json
{"system": "sidekiq", "queues": [{"name": "default", "waiting": 12, "oldest_seconds": 40, "done": 310, "failed": 2}]}
```

| Field | Meaning |
|---|---|
| `waiting` | Jobs that have not started |
| `oldest_seconds` | How long the oldest waiting job has waited |
| `done`, `failed` | Jobs finished and failed since your previous report |
| `max_wait_seconds` | If you cannot see the queue itself: the longest any job waited before it started, since your previous report |

Leave out what you cannot measure. The owner is told when the oldest job has waited 15 minutes, when jobs are waiting and none has finished for 15 minutes, when a queue that reported every minute goes quiet with jobs waiting, and when 5 or more jobs fail in an hour and they are a fifth of all jobs.

## Scheduled jobs

`POST /api/ping/<name>?every=24h`, at the end of the job, after the work has succeeded. `every` is how often it runs (`15m`, `24h`, `7d`). If it goes one and a half times that long without checking in, the owner is told.

## The language version

`PUT /api/runtime` once when the app starts: `{"language": "ruby", "version": "3.3.5"}`. Languages: `node`, `python`, `ruby`, `php`, `go`, `bun`, `deno`. It is checked against end-of-life dates.

## Payments

`POST /api/stripe` with the Stripe event object, from your webhook handler, after you have checked Stripe's signature.

## A one-off check of any site

`GET /api/check?url=yourapp.com` needs no key and is limited to 10 a minute from one address. It loads the home page from outside, checks the certificate and when the domain needs renewing, and looks for obvious holes. Every request is a plain GET a browser could make:

- `http://` should move visitors to `https://`.
- The home page should send `Strict-Transport-Security`, `Content-Security-Policy`, `X-Frame-Options` (or `frame-ancestors`) and `X-Content-Type-Options: nosniff`, and set its cookies `Secure` (and `HttpOnly` for sign-in cookies).
- `/.env`, `/.git/config` and `/.git/HEAD` should not be served. Only the first few KB are read, a file counts only if it reads like the real thing (so a site that answers every address with its home page is not flagged), and what it holds is never stored or returned.
- A framework version shown in `X-Powered-By`, `Server` or `<meta name="generator">` is looked up on OSV.dev.

```json
{"domain": "yourapp.com",
 "working": ["Your site loads (0.4 seconds from here).", "..."],
 "worth_fixing": ["Anyone can download your .env file at yourapp.com/.env. ..."],
 "found": ["Hosted on Vercel.", "Tells visitors it runs Next.js 14.1.0 (in its x-powered-by header)."],
 "security": {
   "checked": ["https_redirect", "headers", "exposed_files", "versions", "cookies"],
   "https_redirect": true,
   "exposed_files": {"/.env": true, "/.git/config": false, "/.git/HEAD": false},
   "software": [{"name": "Next.js", "version": "14.1.0", "where": "x-powered-by header"}],
   "findings": [
     {"level": "broken", "kind": "exposed_file", "path": "/.env", "exposed": true, "plain": "...", "engineer": "..."},
     {"level": "look", "kind": "known_vulnerable_version", "name": "Next.js", "version": "14.1.0", "package": "next",
      "ecosystem": "npm", "fixed": "14.2.25", "advisories": [{"id": "GHSA-f82v-jwr5-mffw", "severity": "critical", "fixed": "14.2.25"}],
      "plain": "...", "engineer": "..."}]},
 "text": "# yourapp.com\n..."}
```

`level` is `broken` (an exposed `.env` or `.git`), `look` (a framework version with known security problems, or no redirect to `https://`) or `minor` (missing headers or cookie flags). Each finding has a `plain` sentence for the owner and an `engineer` sentence for whoever fixes it. `https_redirect` and each entry of `exposed_files` is `null` when the site did not answer that request.
