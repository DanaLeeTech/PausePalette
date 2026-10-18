# PausePalette

**Print in more colours than your AMS has slots.**

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

Try it at **<https://pausepalette.dlee.app>**, or run your own copy — everything below is for that.

## How it works (the short version)

The planner searches for the colour order that needs the fewest pauses, then the
fewest spool swaps, then the fewest colour moves, and finally swaps darker colours
in earlier. Some things it will never do:

- A colour never changes slot mid-print — each spool has one home for the whole
  print.
- Spools you never swap out are loaded first.
- Each pause is placed at the earliest safe tool-change moment, and one pause can
  cover every slot that needs changing.

## Before you start

- A print file from **Bambu Studio** or **OrcaSlicer** — a `.3mf` project, or a
  sliced `.gcode` file.
- A printer with an **AMS** — or a plain single-spool printer, where you set one
  slot and swap by hand at each pause. Bambu Lab printers are supported (see
  **Limits** below).
- **Docker** for the easy install, or **Python 3.10+** if you'd rather not use
  Docker.

## Install and run (Docker — easiest)

```sh
git clone https://github.com/DanaLeeTech/PausePalette.git PausePalette
cd PausePalette
docker compose up -d --build
```

Then open **http://localhost:8000** in your browser. The `-d` runs it in the
background — `docker compose logs -f` follows its logs, and `docker compose down`
stops it.

To reach your instance at a public address, copy `example.env` to `.env` and set
`PAUSEPALETTE_PUBLIC_URL` to that address. It's what the pages use in link
previews and their canonical link; leave it blank for localhost-only use.
`example.env` also has `PAUSEPALETTE_UID` / `PAUSEPALETTE_GID`, so the files in
`./data` end up owned by you.

## How to use it

**Stage 1 — Plan.** Upload your `.3mf` project. PausePalette shows the colour
plan (each colour as a chip in its real colour) and gives you a new `.3mf` to
download with the colours in the planned order.

**Stage 2 — Slice.** Open that new project in Bambu Studio, slice it, and export
the plate file as usual. PausePalette does not slice for you.

**Stage 3 — Add the pauses.** Upload that sliced file back into the same job.
PausePalette checks it matches the plan and gives you the finished file with the
pauses built in, plus a swap checklist to follow at the printer.

Then **print straight from that file — do not slice it again.**

Everything is shown on screen before you print: your AMS slots, how they change
over the print, one card per pause with the swaps it needs, and a tickable
checklist. Every job also has a **Regenerate** button (re-runs your upload — handy
after an update) and a **Delete** button.

## Good to know

- Jobs are kept for **five days**, then deleted.
- Your job list follows your **IP address and browser**, so the same browser on a
  different network sees your jobs, and so does another device on your home
  network.
- Jobs are stored as plain files in the `./data` folder next to PausePalette, so
  you can back them up or move them with a simple copy.
- If you put PausePalette online, add a password in front of it — people sharing
  one internet connection share one job list.

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

And PausePalette won't write anything it isn't sure about: before stage 3 it checks
the slice matches the plan (printer model, filament order, tool changes and
layers), then verifies the edited file is byte-for-byte identical to the original
apart from the recorded insertions, with one pause per place it should be. If a
project's internal layout isn't recognised, it stops and explains rather than
guessing.

## Without Docker

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install ".[web]"
uvicorn webapp.app:app --port 8000
```

## Command line

Prefer a terminal? The same tool has a command line.

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

With Docker, prefix each command with `docker compose run --rm pausepalette`, or
save this alias in `~/.bashrc` to run them as written from any folder (change the
path if you cloned PausePalette elsewhere):

```sh
alias pausepalette='docker compose -f ~/PausePalette/compose.yaml run --rm -v "$PWD:/work" -w /work pausepalette'
```

## For developers

```sh
pip install -e ".[web,dev]"
pytest -q
# regression tests against a real Bambu project (skipped if not present):
PAUSEPALETTE_REAL_3MF="/path/to/keyrings.gcode.3mf" pytest -q tests/test_real_bambu.py
python tools/make_sample.py   # writes samples/Keychain.3mf and .gcode
```

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
  jobstore.py          file-based job store
  cli.py               command line
webapp/                FastAPI + Jinja UI (graphical plan, colour chips)
tests/                 golden tests on a replica of the reference print
```

PausePalette is MIT licensed.
