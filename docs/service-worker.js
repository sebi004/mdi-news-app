// Minimal service worker: caches the app shell (HTML/manifest) so the
// PWA opens instantly on repeat visits. Deliberately does NOT cache
// data/tagged/latest.json — that file changes daily and should always
// be fetched fresh (index.html already fetches it with cache: 'no-store').

const CACHE_NAME = "brief-app-shell-v1";
const APP_SHELL = ["./index.html", "./manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Never cache the data file — always go to network for fresh content.
  if (url.pathname.includes("/data/")) {
    return;
  }

  // App shell: cache-first, falling back to network.
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
