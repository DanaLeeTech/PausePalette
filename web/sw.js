/* PausePalette service worker — the app shell, the Python core, the pinned
 * Pyodide runtime and the webfonts, all cached on the first visit so every
 * visit after it works with no network at all.
 *
 * Cache-first for everything the app needs. The cache name carries the version:
 * bump VERSION and installed copies pick up the new files on their next load.
 */
"use strict";

const VERSION = "v3";
const CACHE = `pausepalette-${VERSION}`;
const BASE = new URL("./", self.location.href);
const at = (path) => new URL(path, BASE).href;

// Must match PYODIDE_VERSION in worker.js: the worker importScripts() this one.
const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.29.5/full/";
const PYODIDE_FILES = [
  "pyodide.js",
  "pyodide.asm.js",
  "pyodide.asm.wasm",
  "python_stdlib.zip",
  "pyodide-lock.json",
].map((name) => PYODIDE + name);

// The stylesheet both pages link. Cached at install together with every woff2
// it names, so the typography survives offline without self-hosting the fonts.
const FONTS =
  "https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap";

// The shell: both pages, every script they run, and the install artwork.
const SHELL = [
  "index.html",
  "job.html",
  "app.js",
  "job.js",
  "render.js",
  "fs.js",
  "worker.js",
  "store.js",
  "style.css",
  "logo.png",
  "icon-180.png",
  "icon-192.png",
  "icon-512.png",
  "icon-maskable.png",
  "manifest.webmanifest",
].map(at);

// Mirrors CORE_FILES in worker.js: the Python the worker mirrors into Pyodide.
const CORE = [
  "pausepalette/__init__.py",
  "pausepalette/colours.py",
  "pausepalette/editor.py",
  "pausepalette/errors.py",
  "pausepalette/gcode.py",
  "pausepalette/model.py",
  "pausepalette/pipeline.py",
  "pausepalette/planner.py",
  "pausepalette/report.py",
  "pausepalette/threemf.py",
  "pausepalette/uploadcheck.py",
  "pausepalette/verifier.py",
  "pausepalette/dialects/__init__.py",
  "pausepalette/dialects/bambu.py",
  "pausepalette/dialects/bambu_models.py",
  "pausepalette/dialects/generic.py",
  "pausepalette/dialects/printer_pauses.py",
].map(at);

/* One cache key per file: the query string is not part of the file.
 * job.html?job=<id> is one page, and style.css?v=N is the file the shell
 * precached — so navigation and the versioned stylesheet both hit. */
function keyFor(url) {
  const path = url.pathname.endsWith("/") ? `${url.pathname}index.html` : url.pathname;
  return new URL(path, url.origin).href;
}

async function precacheFonts(cache) {
  // cors, not the page's opaque no-cors fetch: the body must be readable to
  // find the font files it names
  const response = await fetch(FONTS, { mode: "cors" });
  if (!response.ok) return;
  await cache.put(keyFor(new URL(FONTS)), response.clone());
  const css = await response.text();
  const urls = [...css.matchAll(/url\((https:\/\/fonts\.gstatic\.com\/[^)]+)\)/g)].map(
    (match) => match[1],
  );
  await Promise.allSettled(urls.map((url) => cache.add(url)));
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE);
      await cache.addAll(SHELL.concat(CORE));
      // Pyodide is the big part and the CDN may be unreachable right now: the
      // fetch handler caches it whenever it does arrive.
      await Promise.allSettled(PYODIDE_FILES.map((url) => cache.add(url)));
      await precacheFonts(cache).catch(() => {});
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const name of await caches.keys()) {
        if (name.startsWith("pausepalette-") && name !== CACHE) await caches.delete(name);
      }
      await self.clients.claim();
    })(),
  );
});

async function cached(key) {
  return (await caches.open(CACHE)).match(key);
}

async function page(request) {
  const hit = await cached(keyFor(new URL(request.url)));
  if (hit) return hit;
  return fetch(request); // not cached yet (first visit): the network is all there is
}

async function cacheFirst(request) {
  const key = keyFor(new URL(request.url));
  const hit = await cached(key);
  if (hit) return hit;
  const response = await fetch(request);
  // an opaque response (the Google Fonts stylesheet is no-cors) has status 0
  if (response.ok || response.type === "opaque") {
    await (await caches.open(CACHE)).put(key, response.clone());
  }
  return response;
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);

  if (request.mode === "navigate") {
    event.respondWith(page(request));
    return;
  }
  const wanted =
    url.origin === BASE.origin ||
    url.hostname === "cdn.jsdelivr.net" || // Pyodide
    url.hostname === "fonts.googleapis.com" ||
    url.hostname === "fonts.gstatic.com";
  if (wanted) event.respondWith(cacheFirst(request));
});
