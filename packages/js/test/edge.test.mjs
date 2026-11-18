import assert from 'node:assert/strict';
import http from 'node:http';
import { createRequire } from 'node:module';
import { after, before, test } from 'node:test';

const require = createRequire(import.meta.url);
const seen = [];
let server, base;

// A fresh copy of the library, with nothing configured, as an edge function would load it.
function freshLibrary() {
  delete require.cache[require.resolve('../lib/index.cjs')];
  return require('../lib/index.cjs');
}

before(async () => {
  server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (chunk) => (body += chunk));
    req.on('end', () => {
      seen.push({ path: req.url, auth: req.headers.authorization, body: body ? JSON.parse(body) : null });
      res.writeHead(202).end('{}');
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});
after(() => server.close());

test('a Cloudflare Worker: settings come from env, and the report is kept alive with ctx.waitUntil', async () => {
  const prodbat = freshLibrary();
  const waited = [];
  const ctx = { waitUntil: (p) => waited.push(p) };
  const env = { PRODBAT_KEY: 'pbs_worker', PRODBAT_URL: base };
  const handler = prodbat.wrap(async () => new Response('down', { status: 503 }));

  const response = await handler(new Request('https://yourapp.com/api/pay?x=1', { method: 'POST' }), env, ctx);
  assert.equal(response.status, 503);
  assert.equal(waited.length, 1, 'asked the runtime to wait for the report');
  await Promise.all(waited);
  const report = seen.find((r) => r.path === '/api/errors' && r.auth === 'Bearer pbs_worker');
  assert.deepEqual([report.body.kind, report.body.route], ['HTTP 503', 'POST /api/pay']);
});

test('a Supabase Edge Function: EdgeRuntime.waitUntil keeps logs and reports alive', async () => {
  const prodbat = freshLibrary();
  prodbat.init({ key: 'pbs_supabase', url: base });
  const waited = [];
  globalThis.EdgeRuntime = { waitUntil: (p) => waited.push(p) };
  try {
    const handler = prodbat.wrap(async () => {
      prodbat.log.warn('slow upstream', { ms: 2400 });
      return new Response('ok');
    });
    await handler(new Request('https://xyz.supabase.co/functions/v1/checkout'));
    assert.equal(waited.length, 1, 'log lines are sent before the function is frozen');
    await Promise.all(waited);
    const lines = seen.find((r) => r.path === '/api/logs' && r.auth === 'Bearer pbs_supabase').body.lines;
    assert.deepEqual([lines[0].message, lines[0].level, lines[0].fields], ['slow upstream', 'warning', { ms: 2400 }]);
  } finally {
    delete globalThis.EdgeRuntime;
  }
});

test('nothing to send means nothing to wait for', async () => {
  const prodbat = freshLibrary();
  prodbat.init({ key: 'pbs_quiet', url: base });
  const waited = [];
  await prodbat.wrap(async () => new Response('ok'))(new Request('https://yourapp.com/'), {}, { waitUntil: (p) => waited.push(p) });
  assert.equal(waited.length, 0);
});

test('the ES module entry exports the same functions', async () => {
  const esm = await import('../lib/index.mjs');
  for (const name of ['wrap', 'log', 'ping', 'stripeEvent', 'captureException', 'flush', 'onRequestError']) {
    assert.equal(typeof esm[name], 'function', name);
  }
  assert.equal(typeof esm.default.wrap, 'function');
});
