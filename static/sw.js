/**
 * HiMarket POS - PWA Service Worker (Faz 14)
 * Kesintisiz Çevrimdışı Kasa Çalışması İçin Varlık Önbellekleme
 */

const CACHE_NAME = 'himarket-pos-cache-v2';

const STATIC_ASSETS = [
    '/',
    '/manifest.json',
    '/static/js/offline_engine.js',
    '/static/js/html5-qrcode.min.js',
    'https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css',
    'https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js',
    'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css'
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            // Statik varlıkları sessizce önbelleğe al, bazı CDN kaynakları gecikirse çökmeyi önle
            return Promise.allSettled(
                STATIC_ASSETS.map((url) =>
                    cache.add(url).catch((err) => console.warn('[SW] Cache add failed for:', url, err))
                )
            );
        }).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) => {
            return Promise.all(
                keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
            );
        }).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const request = event.request;
    const url = new URL(request.url);

    // API istekleri, POST, PUT, DELETE veya Django admin çağrıları Service Worker tarafından önbelleklenmez
    if (request.method !== 'GET' || url.pathname.startsWith('/api/') || url.pathname.startsWith('/super-admin/')) {
        return;
    }

    // Statik dosyalar veya CDN kaynakları: Cache First, Network Fallback
    if (url.pathname.startsWith('/static/') || url.origin.includes('cdn.jsdelivr.net') || url.origin.includes('fonts.googleapis.com') || url.origin.includes('fonts.gstatic.com')) {
        event.respondWith(
            caches.match(request).then((cachedResponse) => {
                if (cachedResponse) return cachedResponse;
                return fetch(request).then((networkResponse) => {
                    if (networkResponse && networkResponse.status === 200) {
                        const responseClone = networkResponse.clone();
                        caches.open(CACHE_NAME).then((cache) => cache.put(request, responseClone));
                    }
                    return networkResponse;
                }).catch(() => null);
            })
        );
        return;
    }

    // Navigasyon (Sayfa açılışları): Network First, Fallback to Cache
    if (request.mode === 'navigate') {
        event.respondWith(
            fetch(request).then((networkResponse) => {
                if (networkResponse && networkResponse.status === 200) {
                    const responseClone = networkResponse.clone();
                    caches.open(CACHE_NAME).then((cache) => cache.put(request, responseClone));
                }
                return networkResponse;
            }).catch(() => {
                return caches.match(request).then((cached) => {
                    if (cached) return cached;
                    return caches.match('/');
                });
            })
        );
    }
});
