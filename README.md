# PausePalette

**More colours than your AMS has slots?** PausePalette works out the best colour
plan for a Bambu/Orca print, tells your slicer what filament order to use,
writes that reordered project for you, and then inserts manual spool-swap pauses
into the sliced G-code — the validated `T255` cut-and-retract + `M400 U1`
technique for Bambu AMS printers.

It is fully offline: **no AI, no API keys, no network, no slicer.** All of it is
deterministic parsing and scheduling.

```
STAGE 1  upload a .3mf project
         -> colour plan + a reordered .3mf to download
STAGE 2  in Bambu Studio: open the reordered project, slice, export the plate
         sliced file
STAGE 3  upload that slice back into the same job
         -> verified G-code with the pause blocks inserted + swap checklist
```

A single-stage shortcut (`convert`) does both stages at once for a file that is
already sliced and already in its final filament order.

## Quickstart (Docker)

```sh
docker compose up --build
# open http://localhost:8000
```

The job store lives in a named volume (`pausepalette-data`). To keep job files on
the host instead, swap the volume for `- ./data:/data` in `compose.yaml`.

The same image runs the CLI:

```sh
# stage 1: plan + reordered project
docker compose run --rm pausepalette plan /work/Keychain.3mf --slots 4 \
    -o /work/Keychain_pausepalette.3mf --plan /work/plan.json

# stage 3: insert pauses into the slice the user exported
docker compose run --rm pausepalette apply /work/Keychain.gcode \
    --plan /work/plan.json -o /work/Keychain_pauses.gcode

# single stage
docker compose run --rm pausepalette convert /work/Keychain.gcode --slots 4

# inspect a file
docker compose run --rm pausepalette analyze /work/Keychain.3mf
```

## Without Docker

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install ".[web]"
pausepalette --help
uvicorn webapp.app:app --port 8000
```

## How the plan is made

The planner is an exact search over the classical offline slot-scheduling
problem (a state is the set of colours loaded in the AMS; a pause is a
transition between sets; every demand between two pauses must be covered).
Its objective is lexicographic:

1. **fewest pauses**
2. **fewest spool swaps**
3. **sticky-home tiebreak** — fewest colour moves; a colour changes position
   only when it buys a pause
4. **dark-to-light bias** — darker colours swapped in earlier

And these are hard rules:

- **Boundary carries are preserved** — a colour bridging two layers (e.g. White
  at the end of layer 1 and the start of layer 2) is never evicted before that
  use, and each pause is anchored at the earliest valid tool-change block before
  the colour it brings in.
- **Stock the next section** — the last pause of the artwork lets the body
  colours end up resident, so the rest of the print needs no more swaps.
- **Initial load is darkest-to-lightest** — the four colours the first layers
  need go in Pos1..Pos4 darkest first, and the reordered project's T0..T3 match.

On the reference keychain print this reproduces the six-pause manual plan and
beats it on total swaps.

## Where the reordered project comes from

A project `.3mf` is a zip of XML/JSON, so PausePalette rewrites it directly:

- the filament arrays are permuted into the planned order;
- **every filament reference is remapped** (per-part `filament_id` and painted
  segmentation) so the artwork is colour-identical — only the T numbers move;
- the **stale embedded slice is stripped**, so the old-arrangement G-code can
  never be printed by accident.

If a project's structure is not recognised, PausePalette stops and explains
instead of guessing, and you can reorder the filaments by hand from the plan's
instruction list.

## Verification (stage 3)

The uploaded slice is checked before anything is written. These are hard stops:

- the printer model matches the plan;
- the filament list matches the plan's ordering (so "forgot to reorder" is caught);
- the tool-change sequence and layers match the plan exactly.

Then the edited file is verified:

- byte-identical to the original outside the recorded insertions and edits;
- the tool-change sequence is unchanged;
- exactly one new `M400 U1` per pause, anchored before the right tool change;
- the unload and re-feed steps are disabled (`M620.11 S0`) only in the block
  that follows each pause.

Every output carries: **print from file, never re-slice.** The technique is
validated on a Bambu Lab A1, but each specific file has not been printed — skim
the pause table before you start.

## Scope and assumptions

- **Dialect:** Bambu Studio / OrcaSlicer with AMS (the `T<n>` / `M620` /
  `M620.11` tool-change blocks). Other slicers are future dialect profiles; the
  planner core is slicer-agnostic.
- **Stage 1 needs a sliced project** — the plan needs the exact tool-change
  sequence, which only the slice contains. Slice once in Bambu Studio (the slice
  is saved with the project) and upload that.
- PausePalette never slices and never talks to the printer, so it works with any
  machine that runs the G-code its slicer produced.

## Development

```sh
pip install -e ".[web,dev]"
pytest -q
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
webapp/                FastAPI + Jinja UI
tests/                 golden tests on a replica of the reference print
```
