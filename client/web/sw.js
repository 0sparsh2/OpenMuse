/* OpenMuse service worker (issue #17): app shell cache, share target, web push.
   API responses (/v1/*) are never cached — user data stays on the server. */
const VERSION = "om-shell-v3";
const SHELL = ["/", "/css/tokens.css", "/css/app.css", "/css/chat.css", "/js/openmuse-api.js", "/js/ui.js",
               "/manifest.webmanifest", "/icons/icon-192.png", "/icons/icon-512.png", "/icons/badge-96.png"];
const SHARE_CACHE = "om-share";

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => {
      const upgrading = keys.some((k) => k.startsWith("om-shell-") && k !== VERSION);
      return Promise.all(keys.filter((k) => k !== VERSION && k !== SHARE_CACHE).map((k) => caches.delete(k)))
        .then(() => self.clients.claim())
        .then(async () => {
          // an update: reload open tabs so they run the new app, not the copy they started with
          if (!upgrading) return;
          const wins = await self.clients.matchAll({ type: "window" });
          await Promise.all(wins.map((w) => w.navigate(w.url).catch(() => null)));
        });
    }));
});

async function handleShare(request) {
  const form = await request.formData();
  const cache = await caches.open(SHARE_CACHE);
  await Promise.all((await cache.keys()).map((k) => cache.delete(k)));
  const files = form.getAll("files").filter((f) => f && typeof f === "object" && f.size);
  const meta = { title: form.get("title") || "", text: form.get("text") || "", url: form.get("url") || "",
                 files: files.slice(0, 5).map((f, i) => ({ i, name: f.name || ("shared-" + i), type: f.type || "", size: f.size })),
                 at: Date.now() };
  await cache.put("/__share/meta", new Response(JSON.stringify(meta), { headers: { "Content-Type": "application/json" } }));
  await Promise.all(files.slice(0, 5).map((f, i) => cache.put("/__share/file/" + i, new Response(f, { headers: { "Content-Type": f.type || "application/octet-stream" } }))));
  return Response.redirect("/?share=1", 303);
}

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;
  if (e.request.method === "POST" && url.pathname === "/share") { e.respondWith(handleShare(e.request)); return; }
  if (e.request.method !== "GET") return;
  if (url.pathname === "/v1" || url.pathname.startsWith("/v1/") || url.pathname.startsWith("/vault/")) return;  // live data: network only
  if (e.request.mode === "navigate") {
    // network first so updates land immediately; the cached shell keeps the app opening offline
    e.respondWith(fetch(e.request).then((r) => {
      if (r.ok && url.pathname === "/") { const copy = r.clone(); caches.open(VERSION).then((c) => c.put("/", copy)); }
      return r;
    }).catch(() => caches.match("/")));
    return;
  }
  if (SHELL.includes(url.pathname) || url.pathname.startsWith("/icons/")) {
    // network first, so an update shows on the very next load; the cache keeps it working offline
    e.respondWith(caches.open(VERSION).then(async (c) => {
      try {
        const r = await fetch(e.request, { cache: "no-cache" });
        if (r.ok) c.put(url.pathname, r.clone());
        return r;
      } catch (err) {
        return (await c.match(url.pathname)) || Response.error();
      }
    }));
  }
});

self.addEventListener("push", (e) => {
  let p = {};
  try { p = e.data ? e.data.json() : {}; } catch (err) { p = { title: "OpenMuse", body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(p.title || "OpenMuse", {
    body: p.body || "", tag: p.tag || undefined, renotify: !!p.tag,
    icon: "/icons/icon-192.png", badge: "/icons/badge-96.png",
    requireInteraction: p.kind === "approval",
    data: { url: p.url || "/", note_id: p.note_id || null },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const data = e.notification.data || {};
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) {
      if (new URL(w.url).origin === location.origin) {
        await w.focus();
        w.postMessage({ type: "open-note", note_id: data.note_id });
        return;
      }
    }
    await self.clients.openWindow(data.url || "/");
  })());
});
