# PausePalette

**Print in more colours than your AMS has slots.**

Try it at **<https://danaleetech.github.io/PausePalette/>** — nothing to install.

Your AMS holds a handful of spools — usually four. A model with more colours
normally means pausing the print and swapping spools by hand, which is easy to
get wrong. PausePalette works it out for you:

1. **Plans** the best colour order — fewest pauses, then fewest swaps.
2. **Reorders** the project to match, without touching the design.
3. **Adds the pauses** to the sliced G-code, with a swap checklist.

No AMS? Set one slot and swap by hand at each pause.

## Your files stay on your machine

The site is static: the planner runs in your browser (Python compiled to
WebAssembly), so `.3mf` and `.gcode` files are read, planned and written locally.
Nothing is uploaded, and there is no server to receive it.

## How to use it

1. **Plan.** Choose a `.3mf` project. You get the colour plan and a reordered
   project with the filament list already in the right order.
2. **Slice.** Open that project in Bambu Studio, slice it, export the plate file.
3. **Add pauses.** Upload the sliced file to the same job. PausePalette checks it
   matches the plan, writes the pauses in, and gives you a checklist.

**Print from the file PausePalette gives you — never slice it again.**

Everything is visible before you print: AMS slots and how they change, one card
per pause with its swaps, and a tickable checklist. Jobs have **Regenerate** and
**Delete** buttons.

## Good to know

- Works in all modern browsers. Open/save directly to a folder is Chromium
  desktop only; everywhere else uses the normal picker and download.
- Jobs live in this browser until you delete them — per device, no account, no
  expiry. Safari may clear non-installed site data after 7 days; installing the
  app avoids that. Private windows forget jobs when closed.
- The first visit downloads about 10 MB (the Python runtime), then works offline,
  installed or not.

## Limits

- **Only the Bambu Lab A1 has been test-printed.** Other Bambu models (X1, P1,
  A1 mini, A2L, H2D, H2S, P2S, X2D) and OrcaSlicer/Bambu Studio profiles are
  supported, but check the pause moves before you start.
- PausePalette never slices and never talks to your printer.
- Files it doesn't recognise are refused with an explanation, never guessed at.
  Unsliced projects are fine — the exact plan is completed at stage 3.
- Multi-plate projects are supported; pick the plate. Files up to 400 MB.

Before writing, it checks the slice matches the plan (printer, filament order,
tool changes, layers), then verifies the edited file is byte-identical to the
original apart from the recorded insertions.

## Run your own copy

```sh
docker run --rm -p 8000:8000 ghcr.io/danaleetech/pausepalette:latest
```

Then open <http://localhost:8000>. The browser still does the work — the
container only serves files.

Or clone and use compose (adds the CLI service and an optional `.env`):

```sh
git clone https://github.com/DanaLeeTech/PausePalette.git
cd PausePalette
cp example.env .env          # port, uid/gid, and UI on/off
docker compose up -d --build
```

`PAUSEPALETTE_UI=0` in `.env` makes `docker compose up` start nothing, for a
command-line-only setup.

## Command line

The same tool in a terminal — `pip install .` (Python 3.10+), or in Docker:

```sh
pausepalette plan Keychain.3mf --slots 4 -o Keychain_pausepalette.3mf --plan plan.json
pausepalette apply Keychain.gcode --plan plan.json -o Keychain_pauses.gcode
pausepalette convert Keychain.gcode --slots 4   # sliced, already in final order
pausepalette analyze Keychain.3mf
```

With a clone: `docker compose run --rm cli <args>` — the current folder is
mounted at `/work`. Without one:

```sh
docker run --rm -v "$PWD:/work" -w /work --entrypoint pausepalette \
  ghcr.io/danaleetech/pausepalette:latest plan Keychain.3mf --slots 4
```

## For developers

```sh
pip install -e ".[dev]"
playwright install chromium      # once, for the browser tests
pytest -q
python tools/build_site.py --dev dist && python -m http.server -d dist
python tools/make_sample.py      # samples/Keychain.3mf and .gcode
```

`tests/test_pwa.py` drives the built site in headless Chromium and compares
downloads byte for byte with the Python core. The real-Bambu regression tests run
when `PAUSEPALETTE_REAL_3MF` points at a project.

```
pausepalette/    core library (stdlib only): planner, G-code, 3MF, verifier, CLI
web/             static PWA: Pyodide worker, IndexedDB jobs, service worker
tools/           build_site.py (dist/ for Pages and Docker), icons, sample
tests/           golden tests on a replica of the reference print
```

Bump `VERSION` in `web/sw.js` whenever `web/` changes, or installed copies keep
the old cache.

MIT licensed.
