// Prodbat for your server: Node 18+, Bun, Deno, Supabase Edge Functions,
// Cloudflare Workers and Vercel Edge. No dependencies; only fetch.
//
//   const prodbat = require('prodbat');           // or: import * as prodbat from 'prodbat'
//   app.use(prodbat.express());                   // Express: before the routes
//   app.use(prodbat.expressErrorHandler());       // Express: after the routes
//   export { onRequestError } from 'prodbat';     // Next.js: in instrumentation.ts
//   export const POST = prodbat.wrap(handler);    // any handler that takes a Request and returns a Response
//   Deno.serve(prodbat.wrap(handler));            // Supabase Edge Functions, Deno
//   export default { fetch: prodbat.wrap(handler) }   // Cloudflare Workers (PRODBAT_KEY and PRODBAT_URL as vars)
//
//   prodbat.log('refund issued', { order: 1042 });
//   prodbat.ping('nightly-email', '24h');         // at the end of a scheduled job
//   prodbat.stripeEvent(event);                   // in the Stripe webhook handler
//   prodbat.watchBullMQ([queue]);                 // job queues
//
// Set PRODBAT_KEY and PRODBAT_URL in the environment. Nothing here ever
// throws or holds a request up: reports are sent in the background and
// dropped if Prodbat cannot be reached.
'use strict';

const VERSION = '0.0.1';
const config = { key: null, url: null };
// Cloudflare Workers hand settings to the handler as `env`, not process.env.
let handlerEnv = null;
const pending = new Set();
let logs = [];
let logTimer = null;
let runtimeSent = false;

function init(options = {}) {
  config.key = options.key || null;
  config.url = options.url || null;
}

function fromEnv(name) {
  if (handlerEnv && typeof handlerEnv[name] === 'string') return handlerEnv[name];
  if (typeof process !== 'undefined' && process.env && process.env[name]) return process.env[name];
  try { return globalThis.Deno ? globalThis.Deno.env.get(name) : undefined; } catch { return undefined; } // no --allow-env
}

function settings() {
  return { key: config.key || fromEnv('PRODBAT_KEY'), url: (config.url || fromEnv('PRODBAT_URL') || '').replace(/\/+$/, '') };
}

function deliver(method, path, payload) {
  try {
    const { key, url } = settings();
    if (!key || !url || pending.size >= 50) return;
    const request = fetch(url + path, {
      method,
      headers: { authorization: `Bearer ${key}`, 'content-type': 'application/json', 'user-agent': `prodbat-js/${VERSION}` },
      body: payload === undefined ? undefined : JSON.stringify(payload),
      signal: AbortSignal.timeout(5000),
    }).catch(() => {}).finally(() => pending.delete(request));
    pending.add(request);
  } catch { /* reporting must never become the problem */ }
}

function send(method, path, payload) {
  if (!runtimeSent) {
    runtimeSent = true; // which runtime this really is, once per process
    const v = typeof process !== 'undefined' ? process.versions || {} : {};
    const runtime = v.bun ? ['bun', v.bun] : globalThis.Deno ? ['deno', globalThis.Deno.version.deno] : ['node', v.node];
    if (runtime[1]) deliver('PUT', '/api/runtime', { language: runtime[0], version: runtime[1] });
  }
  deliver(method, path, payload);
}

function flushLogs() {
  logTimer = null;
  while (logs.length) send('POST', '/api/logs', { lines: logs.splice(0, 500) });
}

// Wait for everything queued to go out. Call it before a serverless function returns.
async function flush(timeoutMs = 2000) {
  if (logTimer) { clearTimeout(logTimer); flushLogs(); }
  let timer;
  await Promise.race([Promise.allSettled([...pending]), new Promise((resolve) => { timer = setTimeout(resolve, timeoutMs); })]);
  clearTimeout(timer);
}

// ---- errors ----------------------------------------------------------------

function captureException(err, options = {}) {
  try {
    const error = err instanceof Error ? err : new Error(String(err));
    send('POST', '/api/errors', {
      kind: String(error.name || 'Error').slice(0, 200),
      message: String(error.message || '').slice(0, 1000),
      route: String(options.route || '').slice(0, 300),
      trace: String(error.stack || '').slice(-20000),
      status: 500,
    });
  } catch { /* never throw */ }
}

// A 5xx answer that did not come from an exception.
function captureStatus(status, route = '') {
  if (status >= 500) send('POST', '/api/errors', { kind: `HTTP ${status}`, status, route: String(route).slice(0, 300) });
}

// ---- logs ------------------------------------------------------------------

function log(message, fields, level = 'info') {
  try {
    if (logs.length >= 2000) return;
    logs.push({ message: String(message).slice(0, 4000), level, t: Date.now() / 1000, fields: fields && Object.keys(fields).length ? fields : null });
    if (!logTimer) {
      logTimer = setTimeout(flushLogs, 1000);
      if (logTimer.unref) logTimer.unref();
    }
  } catch { /* never throw */ }
}
log.debug = (message, fields) => log(message, fields, 'debug');
log.info = (message, fields) => log(message, fields, 'info');
log.warn = (message, fields) => log(message, fields, 'warning');
log.error = (message, fields) => log(message, fields, 'error');

// ---- jobs and payments -----------------------------------------------------

// A scheduled job checks in, at the end, after the work has succeeded. every: '15m', '24h', '7d'.
function ping(name, every) {
  send('POST', `/api/ping/${encodeURIComponent(name)}` + (every ? `?every=${encodeURIComponent(every)}` : ''));
}

// Forward a Stripe webhook event, after its signature has been checked.
function stripeEvent(event) {
  send('POST', '/api/stripe', event);
}

// One snapshot of one queue. done and failed are counts since the previous report.
function reportQueue(name, snapshot = {}) {
  send('POST', '/api/queues', { system: snapshot.system || 'custom', queues: [{
    name: String(name), waiting: snapshot.waiting ?? null, oldest_seconds: snapshot.oldestSeconds ?? null,
    done: snapshot.done || 0, failed: snapshot.failed || 0, max_wait_seconds: snapshot.maxWaitSeconds ?? null,
  }] });
}

async function bullmqSnapshot(queue, last) {
  const counts = await queue.getJobCounts('waiting', 'prioritized', 'completed', 'failed');
  const waiting = (counts.waiting || 0) + (counts.prioritized || 0);
  let oldestSeconds = 0;
  if (waiting) {
    // getWaiting(0, 0) is the job that will be taken next, which has waited longest.
    const [next] = await queue.getWaiting(0, 0);
    oldestSeconds = next && next.timestamp ? Math.max(0, (Date.now() - next.timestamp) / 1000) : null;
  }
  const now = { done: counts.completed || 0, failed: counts.failed || 0 };
  const before = last.get(queue.name) || now;
  last.set(queue.name, now);
  return { name: queue.name, waiting, oldest_seconds: oldestSeconds, done: Math.max(0, now.done - before.done), failed: Math.max(0, now.failed - before.failed), max_wait_seconds: null };
}

// Report these BullMQ queues once a minute. Run it in any long-lived process
// that holds the Queue objects; the web server is fine. Returns a function that stops it.
function watchBullMQ(queues, options = {}) {
  const last = new Map();
  const tick = async () => {
    try {
      const snapshots = await Promise.all(queues.map((queue) => bullmqSnapshot(queue, last)));
      send('POST', '/api/queues', { system: 'bullmq', queues: snapshots });
    } catch { /* Redis hiccup: try again next minute */ }
  };
  const timer = setInterval(tick, options.interval || 60000);
  if (timer.unref) timer.unref();
  return () => clearInterval(timer);
}

// ---- web frameworks --------------------------------------------------------

const routeOf = (req) => `${req.method || ''} ${String(req.originalUrl || req.url || '').split('?')[0]}`.trim();

// Express (and Connect): put before the routes. Reports any 5xx answer.
function express() {
  return function prodbatMiddleware(req, res, next) {
    res.on('finish', () => { if (res.statusCode >= 500 && !req.prodbatReported) captureStatus(res.statusCode, routeOf(req)); });
    next();
  };
}

// Express: put after the routes. Reports the exception with its stack, then passes it on.
function expressErrorHandler() {
  return function prodbatErrorHandler(err, req, res, next) {
    req.prodbatReported = true;
    captureException(err, { route: routeOf(req) });
    next(err);
  };
}

// On edge runtimes a function is frozen once it has answered, so anything still
// being sent is lost unless the runtime is asked to wait for it.
function keepAlive(promise, args) {
  const ctx = args.find((a) => a && typeof a.waitUntil === 'function');
  const edge = globalThis.EdgeRuntime && typeof globalThis.EdgeRuntime.waitUntil === 'function' ? globalThis.EdgeRuntime : null;
  try {
    if (ctx) ctx.waitUntil(promise);           // Cloudflare Workers, Vercel Edge
    else if (edge) edge.waitUntil(promise);    // Supabase Edge Functions
  } catch { /* never throw */ }
}

// Wrap a handler that takes a Request and returns a Response: Next.js route
// handlers, Hono, Remix loaders, Supabase Edge Functions (Deno.serve),
// Cloudflare Workers, Vercel Edge, Bun.serve.
function wrap(handler) {
  return async function prodbatWrapped(request, ...rest) {
    const env = rest.find((a) => a && typeof a === 'object' && typeof a.PRODBAT_KEY === 'string');
    if (env) handlerEnv = env;
    let route = '';
    try { route = `${request.method} ${new URL(request.url).pathname}`; } catch { /* not a Request */ }
    try {
      const response = await handler(request, ...rest);
      if (response && response.status >= 500) captureStatus(response.status, route);
      if (pending.size || logs.length) keepAlive(flush(5000), rest);
      return response;
    } catch (err) {
      captureException(err, { route });
      await flush(1000); // a serverless function may be frozen as soon as it throws
      throw err;
    }
  };
}

// Next.js 15+: `export { onRequestError } from 'prodbat'` in instrumentation.ts.
async function onRequestError(err, request) {
  captureException(err, { route: request ? `${request.method || ''} ${request.path || ''}`.trim() : '' });
  await flush(1000);
}

module.exports = { init, flush, captureException, captureStatus, log, ping, stripeEvent, reportQueue, watchBullMQ, bullmqSnapshot, express, expressErrorHandler, wrap, onRequestError };
