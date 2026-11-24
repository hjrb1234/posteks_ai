/* Cache only the public application shell. Never intercept APIs, PDFs, source pages or user data. */
const SHELL_CACHE = 'posteks-shell-v2';
const SHELL_FILES = ['/', '/icon.svg', '/manifest.webmanifest', '/brand/posteks-logo.png'];
self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(caches.open(SHELL_CACHE).then(cache => cache.addAll(SHELL_FILES)));
});
self.addEventListener('activate', event => {
  event.waitUntil(Promise.all([
    self.clients.claim(),
    caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('posteks-') && key !== SHELL_CACHE).map(key => caches.delete(key))))
  ]));
});
self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || request.method !== 'GET' || url.pathname === '/api' || url.pathname.startsWith('/api/')) return;
  const publicAsset = SHELL_FILES.includes(url.pathname) && url.pathname !== '/';
  const builtAsset = url.pathname.startsWith('/assets/') && /\.(js|css|woff2?)$/.test(url.pathname);
  if (publicAsset || builtAsset) {
    event.respondWith(caches.open(SHELL_CACHE).then(async cache => {
      const cached = await cache.match(request);
      if (cached) return cached;
      const response = await fetch(request);
      if (response.ok) await cache.put(request, response.clone());
      return response;
    }));
    return;
  }
  if (request.mode === 'navigate' && !['/service', '/support'].includes(url.pathname)) {
    event.respondWith(fetch(request).then(async response => {
      // Only the universal public HTML shell is persisted, never route-specific pages.
      if (url.pathname === '/' && response.ok && response.headers.get('content-type')?.includes('text/html')) {
        const cache = await caches.open(SHELL_CACHE);
        await cache.put('/', response.clone());
      }
      return response;
    }).catch(async () => (await caches.match('/')) || Response.error()));
  }
});
