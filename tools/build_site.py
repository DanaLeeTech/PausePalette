#!/usr/bin/env python3
"""Assemble dist/: the static PWA plus the core Python it runs in Pyodide.

    python tools/build_site.py [outdir] [--dev]     # default: ./dist

A publish build is exactly what GitHub Pages serves: the app and the core.
--dev adds the worker selftest and samples/ so the end-to-end tests have
fixtures; neither ever reaches the published site.

Deterministic: the same files, byte for byte, every run. Page caching is the
service worker's job, so there are no hashes here.
"""

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The site itself. spike.* is exploration, selftest.* is dev only.
APP = [
    "index.html",
    "job.html",
    "app.js",
    "job.js",
    "render.js",
    "fs.js",
    "worker.js",
    "store.js",
    "sw.js",
    "style.css",
    "logo.png",
    "icon-180.png",
    "icon-192.png",
    "icon-512.png",
    "icon-maskable.png",
    "manifest.webmanifest",
]
DEV = [
    "selftest.html",
    "selftest.js",
    "expected.json",
]

def main():
    parser = argparse.ArgumentParser(description="build the static PWA")
    parser.add_argument("outdir", nargs="?", default=str(ROOT / "dist"))
    parser.add_argument(
        "--dev", action="store_true", help="also include the selftest and samples/"
    )
    args = parser.parse_args()
    out = Path(args.outdir)

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    for name in APP + (DEV if args.dev else []):
        shutil.copy2(ROOT / "web" / name, out / name)
    # the core, laid out as the package so the worker can mirror it into Pyodide
    for path in sorted((ROOT / "pausepalette").rglob("*.py")):
        target = out / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    if args.dev:
        # selftest fixtures; absent in a fresh clone, so the dev build is not gated on them
        if (ROOT / "samples").is_dir():
            shutil.copytree(ROOT / "samples", out / "samples")
    print(f"built {out}" + (" (dev)" if args.dev else ""))


if __name__ == "__main__":
    main()
