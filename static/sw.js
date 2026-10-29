// Rainflow service worker: caches the app shell + static assets, network-first for pages.
const CACHE='rainflow-v1';
const SHELL=['/static/manifest.json','/static/icon-192.png','/static/icon-512.png','/offline'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting()));});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()));});
self.addEventListener('fetch',e=>{
  const u=new URL(e.request.url);
  if(e.request.method!=='GET'||u.origin!==location.origin)return;
  if(u.pathname.startsWith('/static/')||u.hostname==='fonts.gstatic.com'){
    e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request).then(res=>{const cp=res.clone();caches.open(CACHE).then(c=>c.put(e.request,cp));return res;})));return;}
  if(u.pathname.match(/\/(poll|peek|rtc-config|notifications\/poll)/))return; // never cache live data
  e.respondWith(fetch(e.request).catch(()=>caches.match(e.request).then(r=>r||caches.match('/offline'))));
});
self.addEventListener('notificationclick',e=>{e.notification.close();e.waitUntil(clients.openWindow(e.notification.data&&e.notification.data.url||'/'));});
