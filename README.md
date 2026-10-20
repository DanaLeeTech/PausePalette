# PausePalette

**Print in more colours than your AMS has slots.**

Try it at **<https://danaleetech.github.io/PausePalette/>** — nothing to install, and your files
stay on your computer.

Your AMS (the box that feeds filament to a Bambu printer) holds a handful of
spools — usually four. If a model uses more colours than that, you normally have
to pause the print and swap spools by hand, which is easy to get wrong. **No
AMS?** It works with a single spool too — set one slot and swap by hand at each
pause PausePalette tells you about.

PausePalette works all of that out for you. Give it your print file and it:

1. **Plans** the best colour order — the fewest pauses and the fewest spool swaps.
2. **Reorders** your project file to match, without changing your design at all.
3. **Adds the pauses** into the sliced G-code, plus a checklist of which spool to
   swap when.

## Your files never leave your computer

The hosted app is a static site. The planner itself runs inside your browser
(Python compiled to WebAssembly), so your `.3mf` and `.gcode` files are read,
planned and written **on your machine**. Nothing is uploaded, and the site has
no server that could receive them.

## Install it as an app

Open the site and install it — the icon in Chrome/Edge's address bar, or
**Share → Add to Home Screen** on an iPhone or iPad. It then opens in its own
window and keeps working with no internet at all: the app, the planner and the
sample-free interface are cached on the first visit.

## Which browsers

- Works in all modern browsers (Chrome, Edge, Firefox, Safari, on desktop and
  mobile).
- **Open/save straight to a folder** uses the File System Access API: Chromium
  desktop only. Everywhere else the app falls back to a normal file picker and a
  normal download — same result, one extra click.
- Safari may clear site storage that belongs to a website you have not installed
  after 7 days. Installing it as an app avoids that.
- Private/incognito windows throw their storage away when you close them, so
  jobs made there do not survive.

## Jobs live in your browser

Every job (the files, the plan, the checklist) is stored in this browser's own
database until you delete it. There is no account, no server and no expiry —
**Delete** removes one job, **Clear all jobs** removes them all. Clearing site
data removes them too. They are per browser and per device: another browser or
another computer starts with an empty list.

## How it works (the short version)

The planner searches for the colour order that needs the fewest pauses, then the
fewest spool swaps, then the fewest colour moves, and finally swaps darker colours
in earlier. Some things it will never do:

- A colour never changes slot mid-print — each spool has one home for the whole
  print.
- Spools you never swap out are loaded first.
- Each pause is placed at the earliest safe tool-change moment, and one pause can
  cover every slot that needs changing.

## How to use it

**Stage 1 — Plan.** Choose your `.3mf` project. PausePalette shows the colour
plan (each colour as a chip in its real colour) and gives you a new `.3mf` to
save with the colours in the planned order.

**Stage 2 — Slice.** Open that new project in Bambu Studio, slice it, and export
the plate file as usual. PausePalette does not slice for you.

**Stage 3 — Add the pauses.** Choose that sliced file back into the same job.
PausePalette checks it matches the plan and gives you the finished file with the
pauses built in, plus a swap checklist to follow at the printer.

Then **print straight from that file — do not slice it again.**

Everything is shown on screen before you print: your AMS slots, how they change
over the print, one card per pause with the swaps it needs, and a tickable
checklist. Every job also has a **Regenerate** button (re-runs your file — handy
after an update) and a **Delete** button.

## Limits — please read

- **Only the Bambu Lab A1 has actually been printed with these pauses.** Other
  Bambu models (X1, P1, A1 mini, A2L, H2D, H2S, P2S, X2D) and non-Bambu printers
  (via OrcaSlicer/Bambu Studio profiles) are supported, but nobody has test-printed
  them yet — so check the pause moves before you start.
- **Always print from the file PausePalette gives you, and never re-slice it.**
  Re-slicing throws the pauses away. Every output says so on it.
- PausePalette never slices and never talks to your printer, so it works with any
  printer that prints the G-code its slicer produced.
- If your file isn't one PausePalette recognises, it stops and explains why
  instead of guessing. An unsliced project is still accepted — it plans from the
  model's shape, or simply reorders and leaves the exact plan for stage 3.
- Multi-plate projects are supported; pick the plate you're printing (plate 1 by
  default).
- A single file up to 400 MB; the first visit downloads about 10 MB and
  takes a few seconds.

And PausePalette won't write anything it isn't sure about: before stage 3 it checks
the slice matches the plan (printer model, filament order, tool changes and
layers), then verifies the edited file is byte-for-byte identical to the original
apart from the recorded insertions, with one pause per place it should be. If a
project's internal layout isn't recognised, it stops and explains rather than
guessing.

## Run your own copy (Docker)

One image holds both the static site and the command line. The easiest way is
the published image — no clone, no build:

```sh
docker run --rm -p 8000:8000 ghcr.io/danaleetech/pausepalette:latest
```

Then open **http://localhost:8000**. The browser still does all the work — the
container only hands out the files — so a copy on a server is just as private as
the hosted one.

Or clone it and use compose, which adds the `cli` service and an `.env` file:

```sh
git clone https://github.com/DanaLeeTech/PausePalette.git
cd PausePalette
cp example.env .env        # optional: port, uid/gid, and UI on/off
docker compose up -d --build
```

`docker compose logs -f` follows the logs and `docker compose down` stops it.
`example.env` is documented in place; the switch worth knowing is
**`PAUSELETTE_UI`**: `0` starts nothing with `docker compose up`, for a
command-line-only setup. (The CLI is a one-shot command, not a running service,
so there is nothing to switch off — `docker compose run --rm cli …` works either
way.)

## Command line

Prefer a terminal? The same tool has a command line. Install it with
`pip install .` in the clone (Python 3.10+), or run it in the Docker image
without installing anything:

```sh
# plan + write a reordered project
pausepalette plan Keychain.3mf --slots 4 -o Keychain_pausepalette.3mf --plan plan.json

# insert pauses into the slice you exported
pausepalette apply Keychain.gcode --plan plan.json -o Keychain_pauses.gcode

# both at once (file already sliced and in its final colour order)
pausepalette convert Keychain.gcode --slots 4

# look inside a file
pausepalette analyze Keychain.3mf
```

With Docker, prefix each command with `docker compose run --rm cli` — the current
folder is mounted at `/work`, so it reads and writes your files directly. Or save
this alias in `~/.bashrc` to run them as written from any folder (change the path
if you cloned PausePalette elsewhere):

```sh
alias pausepalette='docker compose -f ~/PausePalette/compose.yaml run --rm cli'
```

Without a clone, call the published image directly — same result:

```sh
docker run --rm -v "$PWD:/work" -w /work --entrypoint pausepalette \
  ghcr.io/danaleetech/pausepalette:latest plan Keychain.3mf --slots 4
```

## For developers

```sh
pip install -e ".[dev]"
playwright install chromium     # once, for the browser tests
pytest -q
# regression tests against a real Bambu project (skipped if not present):
PAUSEPALETTE_REAL_3MF="/path/to/keyrings.gcode.3mf" pytest -q tests/test_real_bambu.py

python tools/build_site.py --dev dist && python -m http.server -d dist   # try it locally
python tools/make_sample.py     # writes samples/Keychain.3mf and .gcode
python tools/make_icons.py      # re-draws the app icons from web/logo.png
```

`tests/test_pwa.py` drives the built site in headless Chromium: it plans a real
file, compares the download byte for byte with the Python core, checks the
service worker caches everything, then reloads with the network cut. It needs
network on the first load (the Python runtime comes from a CDN) and skips
cleanly without Chromium.

```
pausepalette/          core library (stdlib only)
  colours.py           offline colour naming + luminance
  gcode.py             Bambu/Orca G-code parser
  planner.py           the slot scheduler
  dialects/bambu.py    pause template + step-D edits
  editor.py            insertion
  verifier.py          independent checks
  threemf.py           project reader / re-writer
  pipeline.py          stage 1 / stage 3 orchestration
  report.py            plan tables, instructions, checklist
  uploadcheck.py       refuse anything that is not a real project or slice
  cli.py               command line
web/                   the static PWA (no build step: plain HTML/CSS/JS)
  worker.js            runs the core in Pyodide, one message per command
  store.js             jobs in IndexedDB
  fs.js                open/save via File System Access, else picker + download
  sw.js                service worker: the whole app, offline
tools/build_site.py    assembles dist/ (what Pages and Docker serve)
tests/                 golden tests on a replica of the reference print
```

**Releasing:** bump `VERSION` in `web/sw.js` whenever the site's files change,
or installed copies keep serving the old ones from their cache.

PausePalette is MIT licensed.
