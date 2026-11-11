// Caches only the app's own files so it opens offline. Prices and API responses are never cached:
// an old quote that looks current is the failure this app must not have.
const CACHE = 'bang-dien-v1'

self.addEventListener('install', () => self.skipWaiting())

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url)
  const scope = new URL(self.registration.scope)
  if (event.request.method !== 'GET' || url.origin !== scope.origin || url.pathname.includes('/api/')) return
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone()
          caches.open(CACHE).then((cache) => cache.put(event.request, copy))
        }
        return response
      })
      .catch(() => caches.match(event.request).then((hit) => hit || caches.match(new URL('./', scope)))),
  )
})
