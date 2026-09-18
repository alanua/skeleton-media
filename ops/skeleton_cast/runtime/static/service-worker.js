const CACHE='home-static-v131';
const ASSETS=['/static/ui.css','/static/app-color-180.png','/static/app-color-192.png','/static/app-color-512.png','/static/manifest.webmanifest','/static/home-shell.js'];
self.addEventListener('install',event=>{event.waitUntil(caches.open(CACHE).then(c=>c.addAll(ASSETS)).catch(()=>{}));self.skipWaiting();});
self.addEventListener('activate',event=>{event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()));});
self.addEventListener('fetch',event=>{const u=new URL(event.request.url);if(event.request.method==='GET'&&u.origin===self.location.origin&&u.pathname.startsWith('/static/')){event.respondWith(caches.match(event.request).then(r=>r||fetch(event.request).then(resp=>{const copy=resp.clone();caches.open(CACHE).then(c=>c.put(event.request,copy));return resp;})));}});
