/* Prodbat. No cookies, no storage. https://github.com/prodbat/prodbat */
(function () {
  var w = window, d = document, l = location, n = navigator, s = d.currentScript;
  if (!s) return;
  var key = s.getAttribute('data-site');
  // Default: /api/e on whatever origin served this script.
  var api = new URL(s.getAttribute('data-api') || '/api/e', s.src).href;
  try {
    if (!key || l.protocol == 'file:' || /^(localhost$|127\.|\[::1\])/.test(l.hostname) || localStorage.prodbat_ignore) return;
  } catch (e) {}

  // A short reference for this page load. Pass it to Stripe as
  // client_reference_id and the payment can be traced to the visit's source.
  var ref = Math.random().toString(36).slice(2, 12);
  w.prodbat = { ref: ref };

  function send(o) {
    o.k = key;
    o.u = l.href;
    var body = JSON.stringify(o);
    if (!(n.sendBeacon && n.sendBeacon(api, body)) && fetch0)
      fetch0(api, { method: 'POST', body: body, keepalive: true, mode: 'no-cors' }).catch(function () {});
  }

  // Page views, including client-side navigation.
  var last;
  function pv() {
    if (l.pathname === last) return;
    // After the first view, the referrer is the page they were just on.
    send({ t: 'pv', i: ref, r: last === undefined ? d.referrer : l.origin + last });
    last = l.pathname;
  }
  var h = w.history, push = h && h.pushState;
  if (push) {
    h.pushState = function () {
      push.apply(this, arguments);
      pv();
    };
    w.addEventListener('popstate', pv);
  }

  // Errors in the page's code. At most 5 per page load.
  var errs = 0;
  function err(m, f, ln, c, st) {
    if (m && errs++ < 5)
      send({ t: 'err', m: String(m).slice(0, 300), f: String(f || '').slice(0, 500), l: ln || 0, c: c || 0, s: String(st || '').slice(0, 2000) });
  }
  w.addEventListener('error', function (e) {
    err(e.message, e.filename, e.lineno, e.colno, e.error && e.error.stack);
  });
  w.addEventListener('unhandledrejection', function (e) {
    var r = e.reason;
    err((r && r.message) || r, '', 0, 0, r && r.stack);
  });

  // Requests to this site's own server that answer 5xx or never answer.
  // At most 10 per page load. Query strings are not sent.
  var reqs = 0, gone;
  w.addEventListener('pagehide', function () { gone = 1; });
  function req(me, url, st) {
    try {
      var x = new URL(url, l.href);
      if (x.origin !== l.origin || x.href.indexOf(api) == 0) return;
      if (st === 0 && (gone || n.onLine === false)) return;
      if ((st === 0 || st >= 500) && reqs++ < 10)
        send({ t: 'req', me: String(me || 'GET').toUpperCase().slice(0, 10), ru: x.pathname.slice(0, 500), st: st });
    } catch (e) {}
  }
  var fetch0 = w.fetch;
  if (fetch0)
    w.fetch = function (a, b) {
      var me = (b && b.method) || (a && a.method), url = (a && a.url) || a;
      return fetch0.apply(this, arguments).then(
        function (r) { req(me, url, r.status); return r; },
        function (e) { if (!(e && e.name == 'AbortError')) req(me, url, 0); throw e; }
      );
    };
  var X = w.XMLHttpRequest;
  if (X) {
    var open = X.prototype.open;
    X.prototype.open = function (me, url) {
      var x = this;
      x.addEventListener('load', function () { req(me, url, x.status); });
      x.addEventListener('error', function () { req(me, url, 0); });
      return open.apply(x, arguments);
    };
  }

  // Stripe Payment Links get the reference added when they are clicked.
  d.addEventListener('click', function (e) {
    var a = e.target && e.target.closest && e.target.closest('a[href*="buy.stripe.com"]');
    if (a && a.href.indexOf('client_reference_id=') < 0)
      a.href += (a.href.indexOf('?') < 0 ? '?' : '&') + 'client_reference_id=' + ref;
  }, true);

  pv();
})();
