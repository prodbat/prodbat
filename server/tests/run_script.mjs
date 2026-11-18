// Runs p.js against a fake browser and prints every beacon it sent as JSON.
// Usage: node run_script.mjs path/to/p.js
import fs from 'node:fs';
import vm from 'node:vm';

const sent = [];
const listeners = {};
const location = { href: 'https://yourapp.com/pricing?utm_source=x', pathname: '/pricing', hostname: 'yourapp.com', protocol: 'https:', origin: 'https://yourapp.com' };
const ctx = {
  document: {
    addEventListener: (type, fn) => (listeners['doc:' + type] ??= []).push(fn),
    referrer: 'https://chatgpt.com/',
    currentScript: { src: 'https://cdn.example/p.js', getAttribute: (a) => (a === 'data-site' ? 'pb_test' : null) },
  },
  location,
  navigator: { onLine: true, sendBeacon: (url, body) => (sent.push({ url, ...JSON.parse(body) }), true) },
  history: { pushState() {} },
  localStorage: {},
  addEventListener: (type, fn) => (listeners[type] ??= []).push(fn),
  fetch: async (url) => {
    if (String(url).includes('offline')) throw new TypeError('Failed to fetch');
    return { status: String(url).includes('boom') ? 500 : 200 };
  },
  URL,
};
ctx.window = ctx;
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);

const go = (path) => { location.pathname = path; location.href = 'https://yourapp.com' + path; ctx.history.pushState({}, '', path); };
go('/signup');
go('/signup'); // same path again: no second page view

listeners.error[0]({ message: 'x is not defined', filename: 'https://yourapp.com/app.js', lineno: 12, colno: 4, error: { stack: 'ReferenceError' } });
listeners.unhandledrejection[0]({ reason: new Error('nope') });

await ctx.fetch('/api/boom?token=secret', { method: 'post' }); // reported
await ctx.fetch('/api/ok'); // fine: not reported
await ctx.fetch('https://other.example/boom'); // another site: not reported
await ctx.fetch('/api/offline').catch(() => {}); // never answered: reported as 0
listeners.pagehide[0]();
await ctx.fetch('/api/offline').catch(() => {}); // page is leaving: not reported

// A click on a Stripe Payment Link gets this page load's reference added.
const link = { href: 'https://buy.stripe.com/abc' };
listeners['doc:click'][0]({ target: { closest: () => link } });

console.log(JSON.stringify({ sent, link: link.href, ref: ctx.prodbat.ref }));
