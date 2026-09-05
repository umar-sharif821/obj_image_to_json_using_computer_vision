/*
 * Service worker for the Legal Metrology Label Scanner.
 *
 * Hand-written rather than generated, so there is no build plugin to keep in
 * step and the caching rules are visible in one short file.
 *
 * Two rules, and the second one matters more than it looks:
 *
 * 1. Static assets are cached stale-while-revalidate, so a returning phone
 *    paints instantly and picks up a new build in the background.
 * 2. **Nothing under /api/ is ever cached or served from cache.** A cached
 *    compliance verdict is worse than no verdict: it would show a finding
 *    against a package the officer is not currently holding. Those requests
 *    always go to the network and are allowed to fail.
 */

const CACHE = 'lm-scanner-v1'
const APP_SHELL = ['/', '/index.html', '/manifest.webmanifest']

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      // A failed precache must not abort activation — the runtime handler will
      // fill the cache on first use anyway.
      .then((cache) => cache.addAll(APP_SHELL).catch(() => undefined))
      .then(() => self.skipWaiting()),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', (event) => {
  const { request } = event
  if (request.method !== 'GET') return

  const url = new URL(request.url)

  // Never cache API traffic, wherever the backend is hosted.
  if (url.pathname.startsWith('/api/')) return
  if (url.origin !== self.location.origin) return

  // Navigations: network first, falling back to the cached shell offline.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone()
          caches.open(CACHE).then((cache) => cache.put(request, copy))
          return response
        })
        .catch(() => caches.match(request).then((hit) => hit || caches.match('/index.html'))),
    )
    return
  }

  // Static assets: serve from cache, refresh in the background.
  event.respondWith(
    caches.match(request).then((hit) => {
      const network = fetch(request)
        .then((response) => {
          if (response && response.status === 200) {
            const copy = response.clone()
            caches.open(CACHE).then((cache) => cache.put(request, copy))
          }
          return response
        })
        .catch(() => hit)
      return hit || network
    }),
  )
})
