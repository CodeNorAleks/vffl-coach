// VFFL Coach service worker: offline shell, fresh data, push alerts.
const SHELL = 'vffl-shell-v2';
const FILES = ['./', './index.html', './manifest.json', './icon.svg', './icon-192.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.endsWith('data.json')) {
    // network first, cached copy if offline
    e.respondWith(fetch(e.request).then(r => { caches.open(SHELL).then(c => c.put('./data.json', r.clone())); return r; })
      .catch(() => caches.match('./data.json')));
    return;
  }
  e.respondWith(caches.match(e.request, { ignoreSearch: true }).then(r => r || fetch(e.request)));
});
self.addEventListener('push', e => {
  let d = { title: 'VFFL Coach', body: '' };
  try { d = e.data.json(); } catch (_) { d.body = e.data ? e.data.text() : ''; }
  const tab = { injury: 'injuries', lineup: 'lineup', waiver: 'waivers', deadline: 'trades', summary: 'coach' }[d.type] || 'lineup';
  e.waitUntil(self.registration.showNotification(d.title, { body: d.body, icon: './icon-192.png', badge: './icon-192.png', data: { tab }, tag: d.type }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const target = new URL('./index.html#' + (e.notification.data?.tab || 'lineup'), location.href).href;
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cs => {
    const c = cs.find(x => x.url.startsWith(location.origin));
    return c ? (c.navigate(target), c.focus()) : self.clients.openWindow(target);
  }));
});
