/* global self */

const CACHE_NAME = 'whisschedule-pwa-v1';

const PRECACHE_URLS = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/offline.html',
  '/icons/icon.svg',
  '/icons/maskable.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    Promise.all([
      caches.keys().then((keys) =>
        Promise.all(
          keys
            .filter((k) => k !== CACHE_NAME)
            .map((k) => caches.delete(k))
        )
      ),
      self.clients.claim(),
    ])
  );
});

function isSameOrigin(requestUrl) {
  try {
    return new URL(requestUrl).origin === self.location.origin;
  } catch {
    return false;
  }
}

self.addEventListener('fetch', (event) => {
  const { request } = event;

  if (request.method !== 'GET') return;
  if (!isSameOrigin(request.url)) return;

  // Network-first for navigations so new deployments show up quickly.
  if (request.mode === 'navigate') {
    event.respondWith(
      (async () => {
        try {
          const fresh = await fetch(request);
          const cache = await caches.open(CACHE_NAME);
          cache.put('/', fresh.clone());
          return fresh;
        } catch {
          const cached = await caches.match(request) || (await caches.match('/')) || (await caches.match('/offline.html'));
          return cached;
        }
      })()
    );
    return;
  }

  // Stale-while-revalidate for other same-origin assets.
  event.respondWith(
    (async () => {
      const cached = await caches.match(request);
      const fetchPromise = fetch(request)
        .then(async (response) => {
          const cache = await caches.open(CACHE_NAME);
          cache.put(request, response.clone());
          return response;
        })
        .catch(() => undefined);

      return cached || (await fetchPromise) || (await caches.match('/offline.html'));
    })()
  );
});

