const CACHE = 'askaria-v2';
const ASSETS = [
  '/',
  '/static/index.html',
  '/static/manifest.json',
  '/static/favicon.ico',
  '/static/favicon.svg',
  '/static/favicon-96x96.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/apple-touch-icon.png',
  '/static/logo.webp',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  // Ne pas cacher les appels API
  if (e.request.url.includes('/auth/') || e.request.url.includes('/getall/') ||
      e.request.url.includes('/stream/') || e.request.url.includes('/img/') ||
      e.request.url.includes('/lyrics') || e.request.url.includes('/playlists') ||
      e.request.url.includes('/search') || e.request.url.includes('/scan') ||
      e.request.method !== 'GET') return;

  e.respondWith(
    caches.match(e.request).then(r => r || fetch(e.request))
  );
});
