/* Helena's Busplan — Service Worker
   Caches app shell on install; serves from cache, updates in background. */

const CACHE_NAME = 'busplan-v7';
const SHELL = [
  './',
  './index.html',
  './style.css',
  './app.js',
  './data-bundle.js',
  './manifest.json',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './design/logo-3.png'
];

self.addEventListener('install', event => {
  event.waitUntil(
    // cache:'reload' umgeht den HTTP-Cache (GitHub Pages liefert max-age=600),
    // sonst landen kurz nach einem Deploy die alten Dateien im neuen Cache.
    caches.open(CACHE_NAME).then(cache =>
      cache.addAll(SHELL.map(url => new Request(url, { cache: 'reload' })))
    )
  );
  self.skipWaiting();
});

// Erlaubt der Seite, ein wartendes Update sofort zu aktivieren
self.addEventListener('message', event => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  // Cache-first for data files; network-first for HTML
  const url = new URL(event.request.url);
  const isData = url.pathname.endsWith('.json');
  const isHtml = url.pathname.endsWith('.html') || url.pathname.endsWith('/');

  if (isHtml) {
    // Network-first so updates are picked up, fallback to cache
    event.respondWith(
      fetch(event.request)
        .then(res => { caches.open(CACHE_NAME).then(c => c.put(event.request, res.clone())); return res; })
        .catch(() => caches.match(event.request))
    );
  } else {
    // Cache-first (data, css, js, icons)
    event.respondWith(
      caches.match(event.request).then(cached => {
        if (cached) {
          // Refresh in background
          fetch(event.request).then(res => {
            if (res.ok) caches.open(CACHE_NAME).then(c => c.put(event.request, res));
          }).catch(() => {});
          return cached;
        }
        return fetch(event.request);
      })
    );
  }
});
