import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import http from 'node:http';
import { createRequire } from 'node:module';
import { after, before, beforeEach, test } from 'node:test';

const prodbat = createRequire(import.meta.url)('../lib/index.cjs');
const seen = [];
let server;

// A stand-in for the Prodbat server.
before(async () => {
  server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (chunk) => (body += chunk));
    req.on('end', () => {
      seen.push({ method: req.method, path: req.url, auth: req.headers.authorization, body: body ? JSON.parse(body) : null });
      res.writeHead(202).end('{}');
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  prodbat.init({ key: 'pbs_test', url: `http://127.0.0.1:${server.address().port}/` });
});
after(() => server.close());
beforeEach(() => { seen.length = 0; });

const sent = (path) => seen.filter((r) => r.path.startsWith(path));

test('the first thing sent is which runtime this is, and an exception carries its stack', async () => {
  prodbat.captureException(new TypeError('x is undefined'), { route: 'GET /api/me' });
  await prodbat.flush();
  assert.deepEqual(sent('/api/runtime')[0].body, { language: 'node', version: process.versions.node });
  const [report] = sent('/api/errors');
  assert.equal(report.auth, 'Bearer pbs_test');
  assert.deepEqual([report.body.kind, report.body.message, report.body.route, report.body.status], ['TypeError', 'x is undefined', 'GET /api/me', 500]);
  assert.match(report.body.trace, /lib\.test\.mjs/);
});

test('log lines go out together with their fields and levels', async () => {
  prodbat.log('refund issued', { order: 1042 });
  prodbat.log.warn('card declined');
  prodbat.log.error('could not send email', { to: 'a@b.co' });
  await prodbat.flush();
  const requests = sent('/api/logs');
  assert.equal(requests.length, 1, 'one request, not one per line');
  assert.deepEqual(requests[0].body.lines.map((l) => [l.message, l.level, l.fields]), [
    ['refund issued', 'info', { order: 1042 }], ['card declined', 'warning', null], ['could not send email', 'error', { to: 'a@b.co' }]]);
});

test('a job checks in, a Stripe event is forwarded and a queue is reported', async () => {
  prodbat.ping('nightly-email', '24h');
  prodbat.stripeEvent({ type: 'invoice.paid' });
  prodbat.reportQueue('emails', { waiting: 3, oldestSeconds: 12, failed: 1, system: 'sidekiq' });
  await prodbat.flush();
  assert.deepEqual(seen.map((r) => r.path), ['/api/ping/nightly-email?every=24h', '/api/stripe', '/api/queues']);
  assert.deepEqual(seen[2].body, { system: 'sidekiq', queues: [{ name: 'emails', waiting: 3, oldest_seconds: 12, done: 0, failed: 1, max_wait_seconds: null }] });
});

test('express: an exception is reported once with its stack, and any other 5xx by its status', async () => {
  const request = (method, url) => ({ method, originalUrl: url });
  const respond = (req, status, err) => {
    const res = new EventEmitter();
    prodbat.express()(req, res, () => {});
    if (err) prodbat.expressErrorHandler()(err, req, res, (passedOn) => assert.equal(passedOn, err));
    res.statusCode = status;
    res.emit('finish');
  };
  respond(request('POST', '/api/checkout?x=1'), 500, new Error('no price'));
  respond(request('GET', '/api/upstream'), 502);
  respond(request('GET', '/api/fine'), 200);
  respond(request('GET', '/api/missing'), 404);
  await prodbat.flush();
  assert.deepEqual(sent('/api/errors').map((r) => [r.body.route, r.body.kind]), [['POST /api/checkout', 'Error'], ['GET /api/upstream', 'HTTP 502']]);
});

test('wrap: a handler that throws or answers 5xx is reported, and its result is passed through', async () => {
  const request = new Request('https://yourapp.com/api/pay?x=1', { method: 'POST' });
  const failing = prodbat.wrap(async () => { throw new RangeError('bad amount'); });
  await assert.rejects(() => failing(request), RangeError);
  const unavailable = await prodbat.wrap(async () => new Response('', { status: 503 }))(request);
  assert.equal(unavailable.status, 503);
  assert.equal(await (await prodbat.wrap(async (req, extra) => new Response(extra))(request, 'ok')).text(), 'ok');
  await prodbat.onRequestError(new Error('render failed'), { method: 'GET', path: '/dashboard' });
  await prodbat.flush();
  assert.deepEqual(sent('/api/errors').map((r) => [r.body.route, r.body.kind]), [
    ['POST /api/pay', 'RangeError'], ['POST /api/pay', 'HTTP 503'], ['GET /dashboard', 'Error']]);
});

test('BullMQ queues are read through the calls BullMQ offers, and only the change is sent', async () => {
  const queue = {
    name: 'emails', counts: { waiting: 2, prioritized: 1, completed: 10, failed: 2 },
    getJobCounts: async () => queue.counts,
    getWaiting: async (start, end) => { assert.deepEqual([start, end], [0, 0]); return [{ timestamp: Date.now() - 90_000 }]; },
  };
  const last = new Map();
  const first = await prodbat.bullmqSnapshot(queue, last);
  assert.deepEqual([first.waiting, first.done, first.failed], [3, 0, 0]);
  assert.ok(first.oldest_seconds >= 89 && first.oldest_seconds <= 92);
  queue.counts = { waiting: 0, prioritized: 0, completed: 25, failed: 3 };
  assert.deepEqual(await prodbat.bullmqSnapshot(queue, last), { name: 'emails', waiting: 0, oldest_seconds: 0, done: 15, failed: 1, max_wait_seconds: null });
});

test('nothing throws when Prodbat cannot be reached or is not configured', async () => {
  prodbat.init({ key: 'pbs_test', url: 'http://127.0.0.1:1' });
  prodbat.ping('job'); prodbat.log('hello'); prodbat.captureException('just a string');
  await prodbat.flush();
  prodbat.init({});
  prodbat.ping('job'); prodbat.log('hello');
  await prodbat.flush();
});
