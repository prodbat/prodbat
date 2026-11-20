# prodbat (JavaScript)

Monitoring for apps built with AI. This package is two things: the `npx prodbat` command line, which sets a project up and lets you or your coding agent ask what is wrong with the live site, and a library that sends a server's errors, log lines, scheduled-job check-ins, queue health and Stripe events to [Prodbat](https://github.com/prodbat/prodbat). The library has no dependencies (it only uses `fetch`), never throws, and never holds a request up: reports go out in the background and are dropped if Prodbat can't be reached.

Works on Node 18+, Bun, Deno, Supabase Edge Functions, Cloudflare Workers and Vercel Edge.

## Set up

```sh
npx prodbat init --email you@example.com
```

This creates the project on hosted Prodbat (prodbat.com), writes `PRODBAT_KEY` and `PRODBAT_URL` to `.env`, and prints the script tag for your pages. Prodbat emails that address a link to activate the dashboard; the alerts go there too. `npx prodbat setup --all` is the checklist your agent works through: server errors, security alerts for your packages, logs, payments, scheduled jobs and the job queue. It makes every change, ships them together the way your project usually ships (a pull request or a deploy), and runs `npx prodbat done` with the items it finished. init takes off the list what your app shows no sign of; `npx prodbat skip <id>` skips anything else your app doesn't have. init also writes the Prodbat skill to `.claude/skills/prodbat/SKILL.md` and a short Prodbat section to `AGENTS.md`, and sets up Prodbat's MCP server for Claude Code (`.mcp.json`) and Cursor (`.cursor/mcp.json`), which read `PRODBAT_KEY` from the environment the agent was started from. On your own server, add `--server <url> --token <admin token>`. To check any site from outside without an account: `npx prodbat check yoursite.com`.

## The command line

```sh
npx prodbat setup          # the setup checklist: what is done, and the steps for the next item (--all: every item left)
npx prodbat done S-03 S-05 # after making several setup changes and shipping them together: counts them all
npx prodbat skip <id>      # skip a setup item your app doesn't have (no payments, no scheduled jobs)
npx prodbat status         # what is wrong with the live site right now, with evidence
npx prodbat next           # the most important thing to fix, as one task with steps
npx prodbat done <id>      # after a fix: Prodbat checks from outside whether it worked
npx prodbat verify         # after a deploy: does the site load, is data arriving
npx prodbat logs [text]    # what the app logged; --level error, --since 1h
npx prodbat deps           # package versions, for security and end-of-life alerts
npx prodbat test-alert     # send yourself a test alert
```

`npx prodbat help` lists everything. Add `--json` to any command for output a coding agent can read.

## The library

```sh
npm install prodbat
```

```js
import * as prodbat from 'prodbat';             // or: const prodbat = require('prodbat')

// Catch errors, where your app answers requests:
app.use(prodbat.express());                     // Express: before the routes
app.use(prodbat.expressErrorHandler());         // Express: after the routes
export { onRequestError } from 'prodbat';       // Next.js: in instrumentation.ts
export const POST = prodbat.wrap(handler);      // any handler that takes a Request and returns a Response
Deno.serve(prodbat.wrap(handler));              // Supabase Edge Functions, Deno
export default { fetch: prodbat.wrap(handler) } // Cloudflare Workers (PRODBAT_KEY and PRODBAT_URL as vars)

// Also:
prodbat.log('refund issued', { order: 1042 });  // a log line, with fields (also .info, .warn, .error)
prodbat.ping('nightly-email', '24h');           // at the end of a scheduled job, after it succeeded
prodbat.stripeEvent(event);                     // in the Stripe webhook, after the signature check
prodbat.watchBullMQ([queue]);                   // BullMQ: queue size, oldest wait, failures, every minute
prodbat.reportQueue('emails', { waiting: 3, oldestSeconds: 12 });   // any other queue, once a minute
prodbat.captureException(err, { route: 'POST /x' });               // report one yourself
```

Set `PRODBAT_KEY` and `PRODBAT_URL` in the environment, or call `prodbat.init({ key, url })`. In short-lived functions, `await prodbat.flush()` before returning if you aren't using `wrap`.

## Tests

```sh
npm test
```

MIT licence.
