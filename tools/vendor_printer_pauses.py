"""Regenerate ``pausepalette/dialects/printer_pauses.py`` from slicer profiles.

    python tools/vendor_printer_pauses.py \
        <OrcaSlicer>/resources/profiles \
        "<BambuStudio>/share/BambuStudio/profiles"

Every slicer ships a printer profile per machine, and those profiles say how
that machine pauses and how it changes filament:

* ``machine_pause_gcode``      — what the slicer writes for "pause here"
  (``M601`` on Prusa, ``PAUSE`` on Klipper, ``M25`` on Creality, ...);
* ``change_filament_gcode``    — what it writes for a colour change, empty for
  most machines, in which case the slicer simply emits ``T<n>``.

PausePalette writes the same commands, so a pause works the way that machine
expects instead of assuming Bambu. Identical templates are stored once.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path


def flatten(path: str, index: dict[str, str], seen: tuple[str, ...] = ()) -> dict:
    """Resolve a profile's ``inherits`` chain into one flat config."""
    if path in seen:
        return {}
    try:
        cfg = json.load(open(path))
    except (OSError, ValueError):
        return {}
    out: dict = {}
    parent = cfg.get("inherits")
    if parent:
        same_dir = os.path.join(os.path.dirname(path), parent + ".json")
        if os.path.exists(same_dir):
            out.update(flatten(same_dir, index, seen + (path,)))
        elif parent in index:
            out.update(flatten(index[parent], index, seen + (path,)))
    out.update(cfg)
    return out


def index_profiles(roots: list[str]) -> dict[str, str]:
    index: dict[str, str] = {}
    for root in roots:
        for f in glob.glob(f"{root}/**/*.json", recursive=True):
            try:
                name = json.load(open(f)).get("name")
            except (OSError, ValueError):
                continue
            if name:
                index.setdefault(name, f)
    return index


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    roots = sys.argv[1:]
    index = index_profiles(roots)

    def text(value) -> str:
        """A profile value as text: some are lists of lines."""
        if isinstance(value, list):
            value = "\n".join(str(v) for v in value)
        return str(value or "").strip()

    def usable_change(value: str) -> str:
        """A change-filament command we can drop straight into a file.

        Some machines carry a whole templated toolchange with slicer
        placeholders; those cannot be written by us (and the file already has
        the resolved version), so only plain commands are kept.
        """
        if not value or "{" in value or "[" in value:
            return ""
        first = value.splitlines()[0].strip()
        return value if first and not first.startswith(";") else ""

    # printer model -> (pause gcode, a plain change-filament command if any)
    found: dict[str, tuple[str, str]] = {}
    for root in roots:
        for f in sorted(glob.glob(f"{root}/*/machine/*.json")):
            try:
                raw = json.load(open(f))
            except (OSError, ValueError):
                continue
            model = str(raw.get("printer_model") or raw.get("name") or "").strip()
            if not model or model.lower().endswith("template change_filament_gcode"):
                continue
            cfg = flatten(f, index)
            pause = text(cfg.get("machine_pause_gcode"))
            change = usable_change(text(cfg.get("change_filament_gcode")))
            if not pause and not change:
                continue
            if model not in found or (len(pause) + len(change)) < sum(len(x) for x in found[model]):
                found[model] = (pause, change)

    templates: list[tuple[str, str]] = []
    table: dict[str, int] = {}
    for model, pair in sorted(found.items()):
        if pair not in templates:
            templates.append(pair)
        table[model] = templates.index(pair)

    out = Path(__file__).resolve().parents[1] / "pausepalette" / "dialects" / "printer_pauses.py"
    body = [
        '"""How each printer pauses, taken from the slicers\' own profiles.',
        "",
        "Do not edit by hand — regenerate with::",
        "",
        f"    python {Path(__file__).name} <OrcaSlicer>/resources/profiles <BambuStudio>/profiles",
        "",
        "Per printer, from its own slicer profile:",
        "",
        "* ``machine_pause_gcode`` — what the slicer writes for \"pause here\"",
        "  (``M601`` on Prusa, ``PAUSE`` on Klipper, ``M25`` on Creality, ``M400 U1``",
        "  on Bambu);",
        "* ``change_filament_gcode``, when it is a plain command (``M600`` on an",
        "  Ender): that one unloads, parks, waits and loads, so it is the better",
        "  thing to write at a manual spool swap. Templated multi-tool sequences are",
        "  dropped — those machines' sliced files already contain the resolved",
        "  version.",
        "",
        "The colour-change G-code is not vendored: the sliced file already has that",
        "machine's own resolved change sequence (usually a bare ``T<n>``), and the",
        "pause is inserted in front of it.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "#: (machine_pause_gcode, plain change_filament_gcode or '')",
        "TEMPLATES: list[tuple[str, str]] = [",
    ]
    for pause, change in templates:
        body.append(f"    ({pause!r}, {change!r}),")
    body += [
        "]",
        "",
        "#: printer model -> index into TEMPLATES",
        "PRINTERS: dict[str, int] = {",
    ]
    for model, idx in sorted(table.items()):
        body.append(f"    {model!r}: {idx},")
    body += [
        "}",
        "",
        "",
        "def pause_template(printer_model: str | None) -> tuple[str, str] | None:",
        '    """The (pause, change-filament) pair for a printer, or None."""',
        "    if not printer_model:",
        "        return None",
        "    name = printer_model.strip()",
        "    idx = PRINTERS.get(name)",
        "    if idx is None and not name.startswith(\"Bambu Lab \"):",
        '        idx = PRINTERS.get(f\"Bambu Lab {name}\")',
        "    if idx is None:",
        "        return None",
        "    return TEMPLATES[idx]",
        "",
    ]
    out.write_text("\n".join(body))
    print(f"wrote {out}: {len(table)} printers, {len(templates)} distinct templates")
    for pause, change in sorted(set(templates))[:20]:
        print(f"   pause={pause!r:34s} change={change!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
