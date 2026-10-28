// Service worker: stale-while-revalidate app shell. API responses are handled by app.js (ETag + localStorage).
// config.js is NEVER cached: it differs per environment (local emulator vs AWS) and a stale copy breaks sign-in.
const SHELL = 'agriprice-shell-v3';
const ASSETS = ['/', '/index.html', '/styles.css', '/app.js', '/manifest.webmanifest', '/icon.svg'];
const NEVER_CACHE = ['/config.js', '/sw.js'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  // drop every older cache (including copies of config.js saved by earlier versions)
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (NEVER_CACHE.includes(url.pathname) || /^\/(api|analytics|dev|ussd|sms)\//.test(url.pathname + '/')) return;
  e.respondWith(
    caches.open(SHELL).then(async (cache) => {
      const cached = await cache.match(e.request, { ignoreSearch: true });
      const network = fetch(e.request).then((res) => { if (res.ok) cache.put(e.request, res.clone()); return res; }).catch(() => cached);
      return cached || network;
    })
  );
});
