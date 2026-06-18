// Spliti service worker — gives the app an installable, standalone shell on
// Android (manifest) and iOS (apple-touch-icon + standalone meta). Strategy:
//   - same-origin GET navigations: network-first, fall back to cached shell
//     (so the app opens offline; live data still needs the network).
//   - static assets (icons/manifest): cache-first.
//   - everything else (the /api/* JSON, AI streams): straight to network.
const VERSION = 'spliti-v6';
const SHELL = [
  '/',
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/maskable-512.png',
  '/icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION).then((cache) =>
      // Best-effort: a 401 (basic auth) on '/' shouldn't abort the install.
      Promise.allSettled(SHELL.map((url) => cache.add(url)))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

const isStatic = (url) =>
  url.pathname.startsWith('/icons/') || url.pathname === '/manifest.webmanifest';

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Cache-first for static, immutable-ish assets.
  if (isStatic(url)) {
    event.respondWith(
      caches.match(request).then((hit) =>
        hit || fetch(request).then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(VERSION).then((c) => c.put(request, copy));
          }
          return res;
        })
      )
    );
    return;
  }

  // Network-first for page navigations, fall back to the cached shell offline.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(VERSION).then((c) => c.put('/', copy));
          }
          return res;
        })
        .catch(() => caches.match('/').then((hit) => hit || caches.match(request)))
    );
    return;
  }

  // Everything else (API/JSON/streams): network, no caching.
});

// ---- Web Push ----
// Always show a banner so the event lands in the notification tray, and also nudge
// any open window to refresh in place (the page reuses its normal flush()), so a
// member looking at the app sees the change live as well as in the tray.
self.addEventListener('push', (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (_) { /* keep defaults */ }
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of wins) c.postMessage({ type: 'sync' });
    await self.registration.showNotification(d.title || 'Spliti', {
      body: d.body || '',
      icon: '/icons/icon-192.png',
      badge: '/icons/icon-192.png',
      tag: d.tag || 'spliti',
      data: { url: d.url || '/' },
    });
  })());
});

// Tapping a notification focuses an already-open window (and nudges it to sync)
// or opens the app.
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of wins) {
      if ('focus' in c) {
        c.postMessage({ type: 'sync' });
        return c.focus();
      }
    }
    if (self.clients.openWindow) return self.clients.openWindow(url);
  })());
});
