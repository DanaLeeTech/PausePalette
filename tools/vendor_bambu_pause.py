"""Regenerate ``pausepalette/dialects/bambu_models.py`` from Bambu Studio.

    python tools/vendor_bambu_pause.py /path/to/BambuStudio/profiles

The profiles ship with Bambu Studio (flatpak:

    /var/lib/flatpak/app/com.bambulab.BambuStudio/current/active/files/
        share/BambuStudio/profiles

or the AppImage, under ``resources/profiles``).

Each Bambu printer's ``machine_end_gcode`` contains a "pull back filament to
AMS" sequence — the unload the printer performs at the end of a print. That is
exactly what a manual spool swap needs before the user reaches into the AMS, so
PausePalette uses it, per printer, instead of assuming the A1's moves.

We keep only the literal commands: lines carrying a slicer placeholder or a
conditional are dropped, and the numbers in them (retraction distances, flush
speeds) come from the sliced file's own tool-change block instead.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from pathlib import Path

MARKER = "pull back filament to AMS"
START_RE = re.compile(r"^M620 S\d+")
END_RE = re.compile(r"^M621 S\d+")


def unload_sequences(end_gcode: str) -> list[str]:
    """Every M620…M621 unload group after the 'pull back' marker."""
    lines = [l.strip() for l in end_gcode.split("\n")]
    try:
        start = next(i for i, l in enumerate(lines) if MARKER in l)
    except StopIteration:
        return []

    groups: list[list[str]] = []
    current: list[str] | None = None
    for line in lines[start + 1 :]:
        if not line or line.startswith(";"):
            continue
        if "{" in line or "[" in line:
            # a slicer conditional or placeholder we cannot resolve here
            continue
        if START_RE.match(line):
            if current:
                groups.append(current)
            current = [line]
            continue
        if current is None:
            continue
        current.append(line)
        if END_RE.match(line):
            groups.append(current)
            current = None
            break
    if current:
        groups.append(current)
    return [line for group in groups for line in group]


def model_name(path: str) -> str:
    return re.sub(r" 0\.4 nozzle.*", "", os.path.basename(path)).replace("Bambu Lab ", "")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    profiles = Path(sys.argv[1])
    machine = profiles / "BBL" / "machine"
    if not machine.is_dir():
        print(f"no BBL machine profiles under {machine}")
        return 1

    table: dict[str, list[str]] = {}
    for path in sorted(glob.glob(str(machine / "Bambu Lab*0.4 nozzle template machine_end_gcode.json"))):
        gcode = json.load(open(path)).get("machine_end_gcode", "")
        seq = unload_sequences(gcode)
        if seq:
            table[model_name(path)] = seq

    out = Path(__file__).resolve().parents[1] / "pausepalette" / "dialects" / "bambu_models.py"
    body = [
        '"""Per-printer pause sequences, generated from Bambu Studio\'s profiles.',
        "",
        "Do not edit by hand — regenerate with::",
        "",
        f"    python {Path(__file__).name} <BambuStudio>/profiles",
        "",
        "Each entry is that printer's own \"pull back filament to AMS\" sequence, taken",
        "from its ``machine_end_gcode``: the unload it performs at the end of a print.",
        "A manual spool swap needs the same thing, so the pause block wraps it with a",
        "Z lift, the fans off and the wait.",
        "",
        "Only the A1 has been validated on hardware. The others are the printer's own",
        "G-code, but nobody has printed them from PausePalette yet.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "# printer model (as written in the G-code) -> the unload lines",
        "BAMBU_UNLOAD: dict[str, list[str]] = {",
    ]
    for model, seq in sorted(table.items()):
        body.append(f'    {model!r}: [')
        for line in seq:
            body.append(f"        {line!r},")
        body.append("    ],")
    body += [
        "}",
        "",
        "# what a sliced file may call the same machine",
        "ALIASES: dict[str, str] = {",
        '    "Bambu Lab X1C": "X1 Carbon",',
        '    "Bambu Lab A1 mini": "A1 mini",',
        '    "Bambu Lab A1M": "A1 mini",',
        "}",
        "",
        "",
        "def unload_for(printer_model: str | None) -> list[str] | None:",
        '    """The unload sequence for a printer model, or None if unknown."""',
        "    if not printer_model:",
        "        return None",
        "    name = printer_model.strip()",
        "    if name.startswith(\"Bambu Lab \"):",
        "        name = name[len(\"Bambu Lab \"):]",
        "    name = ALIASES.get(f\"Bambu Lab {name}\", name)",
        "    return BAMBU_UNLOAD.get(name)",
        "",
    ]
    out.write_text("\n".join(body))
    print(f"wrote {out} ({len(table)} printers)")
    for model in sorted(table):
        print(f"   {model:14s} {len(table[model])} lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
