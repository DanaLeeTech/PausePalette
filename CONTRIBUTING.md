# Contributing

Small, focused changes are welcome. Open an issue before a large one.

## Setting up

Python 3.10 or newer:

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium      # once, for the browser tests
pytest -q
```

`tests/test_pwa.py` drives the built site in headless Chromium and needs Playwright
plus network on the first load (Pyodide comes from a CDN). The real-project tests
wait for `PAUSEPALETTE_REAL_3MF=/path/to/project.3mf`; both skip when it is unset.

## The site

`web/` is plain HTML, CSS and JavaScript; no bundler, so edit and reload:

```sh
python tools/build_site.py --dev dist
python -m http.server -d dist 8123     # scratch port, not 8000
```

Open <http://localhost:8123>. `--dev` adds the worker selftest and `samples/` and is
never published. `python tools/make_sample.py` writes a project to try.

**Bump `VERSION` in `web/sw.js` whenever `web/` or `pausepalette/` changes**, or
returning visitors keep the old files out of their cache.

## Commits

One line, `area: change`, for example:

```
web: keep slicer pauses and say so in step 3
plan: let the first layer's colour order be pinned position by position
threemf: 0 in a filament ref is "default", not filament 0
compose: correct the PAUSEPALETTE_* variable names
```

Tests pass before you push. MIT licensed.
