// Prodbat's service worker: shows alerts as notifications, and opens the
// project's page when one is tapped. Served at /d/sw.js; each project's page
// registers it with that page as its scope.

self.addEventListener('push', function (event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) {}
  // A problem's notifications share a tag, so "Working again" replaces "Broken".
  event.waitUntil(self.registration.showNotification(data.title || 'Prodbat', {
    body: data.body || '',
    tag: data.tag || 'prodbat',
    renotify: true,
    icon: data.icon,
    data: { url: data.url || self.registration.scope }
  }));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || self.registration.scope;
  event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (open) {
    for (var i = 0; i < open.length; i++) {
      if (open[i].url.split('?')[0].split('#')[0] === url && 'focus' in open[i]) return open[i].focus();
    }
    return clients.openWindow(url);
  }));
});
