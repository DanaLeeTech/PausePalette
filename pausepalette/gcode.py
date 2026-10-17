"""Parser for Bambu Studio / OrcaSlicer G-code.

Reads the file as text without normalising line endings so the original bytes
can be reproduced exactly when editing. Only standard-library code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import GcodeFile, LayerMarker, ToolChange

# --- patterns -------------------------------------------------------------

RE_HEADER_KV = re.compile(r"^;\s*([A-Za-z0-9_ /()-]+?)\s*[:=]\s*(.*?)\s*$")
RE_FILAMENT_COLOUR = re.compile(r"^;\s*filament_colour\s*=\s*(.*)$", re.I)
RE_PRINTER_MODEL = re.compile(r"^;\s*printer_model\s*=\s*(.*)$", re.I)
RE_LAYER_NUM_TOTAL = re.compile(r"^;\s*layer num/total_layer_count:\s*(\d+)\s*/\s*(\d+)", re.I)
RE_LAYER_NUM = re.compile(r"^;\s*(?:layer num|current_layer|layer)\s*[:=]\s*(\d+)\s*$", re.I)
RE_LAYER_CHANGE = re.compile(r"^;\s*LAYER_CHANGE", re.I)
RE_SECTION = re.compile(r"^;=+")
RE_M620 = re.compile(r"^M620 S(\d+)A\s*(?:;.*)?$")
RE_M621 = re.compile(r"^M621 S(\d+)A\s*(?:;.*)?$")
RE_T_COMMAND = re.compile(r"^T(\d+)\s*(?:;.*)?$")
RE_M620_11_UNLOAD = re.compile(r"^M620\.11 S1 I(\d+) E-\d")
RE_M620_11_FEED = re.compile(r"^M620\.11 S1 I(\d+) E\d")
RE_M629 = re.compile(r"^M629 S1\s*(?:;.*)?$")
RE_DISABLED = re.compile(r"^M620\.11 S0(?:\s*;.*)?$")
RE_G1_Z = re.compile(r"^G1 Z([\d.]+) F(\d+)")
RE_M104 = re.compile(r"^M104 S(\d+)")

# The re-feed group that follows an unload line (Bambu/Orca shape).
REFED_TAIL = [
    re.compile(r"^M620\.11 S1 I\d+ E\d"),
    re.compile(r"^M628 S1\s*(?:;.*)?$"),
    re.compile(r"^G92 E0\s*(?:;.*)?$"),
    re.compile(r"^G1 E[\d.]+"),
    re.compile(r"^M400\s*(?:;.*)?$"),
    re.compile(r"^M629 S1\s*(?:;.*)?$"),
]

TOOL_WINDOW = 60  # how far a block may extend from its M620 line


@dataclass
class ParseError(Exception):
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


def split_lines(text: str) -> list[str]:
    """Split keeping line endings, so round-tripping is byte exact."""
    return text.splitlines(keepends=True)


def _strip(line: str) -> str:
    return line.strip()


def parse_filaments(line: str) -> list[str]:
    payload = line.split("=", 1)[1].strip()
    parts = [p.strip() for p in payload.split(";")]
    return [p for p in parts if p]


def parse_gcode(text: str) -> GcodeFile:
    lines = split_lines(text)
    gf = GcodeFile(lines=lines)

    # --- header -----------------------------------------------------------
    for i, raw in enumerate(lines):
        line = _strip(raw)
        if line.startswith("; HEADER_BLOCK_END") or line.startswith("; CONFIG_BLOCK_END"):
            continue
        m = RE_FILAMENT_COLOUR.match(line)
        if m and not gf.filaments:
            gf.filaments = parse_filaments(line)
            gf.header["filament_colour"] = m.group(1)
            continue
        m = RE_PRINTER_MODEL.match(line)
        if m and gf.printer_model is None:
            gf.printer_model = m.group(1).strip()
            gf.header["printer_model"] = gf.printer_model
            continue
        for key in (
            "machine_pause_gcode",
            "change_filament_gcode",
            "printer_settings_id",
            "total filament",
            "total_filament",
            "nozzle_diameter",
            "layer_height",
        ):
            pat = re.compile(rf"^;\s*{re.escape(key)}\s*=\s*(.*)$", re.I)
            mm = pat.match(line)
            if mm:
                gf.header.setdefault(key, mm.group(1).strip())
        if line.startswith("; CONFIG_BLOCK_END") or (
            gf.filaments and gf.printer_model and line.startswith("; EXECUTABLE_BLOCK_START")
        ):
            break
        # stop scanning for the header after the executable block begins
        if line.startswith("; EXECUTABLE_BLOCK_START"):
            break

    # --- layer markers and tool changes ----------------------------------
    layer = 1
    total_layers = None
    markers: list[LayerMarker] = []
    for i, raw in enumerate(lines):
        line = _strip(raw)
        m = RE_LAYER_NUM_TOTAL.match(line)
        if m:
            layer = int(m.group(1))
            total_layers = int(m.group(2))
            markers.append(LayerMarker(line=i, layer=layer, total=total_layers))
            continue
        m = RE_LAYER_NUM.match(line)
        if m:
            layer = int(m.group(1))
            markers.append(LayerMarker(line=i, layer=layer, total=None))
            continue
        if RE_LAYER_CHANGE.match(line):
            layer += 1
            markers.append(LayerMarker(line=i, layer=layer, total=None))
    gf.layers = markers
    gf.header["total_layer_count"] = str(total_layers) if total_layers else ""

    layer_at: dict[int, int] = {}
    current = 1
    marker_by_line = {m.line: m.layer for m in markers}
    for i in range(len(lines)):
        if i in marker_by_line:
            current = marker_by_line[i]
        layer_at[i] = current

    # --- tool-change blocks ----------------------------------------------
    blocks: list[ToolChange] = []
    ordinal = 0
    i = 0
    n = len(lines)
    while i < n:
        m = RE_M620.match(_strip(lines[i]))
        if not m:
            i += 1
            continue
        window_end = min(n, i + TOOL_WINDOW)
        # find the T<n> and M621
        tool = None
        m621 = None
        for j in range(i + 1, window_end):
            st = _strip(lines[j])
            tm = RE_T_COMMAND.match(st)
            if tm and tool is None:
                tool = int(tm.group(1))
            if RE_M621.match(st):
                m621 = j
                break
        if tool is None or m621 is None:
            i += 1
            continue
        # header line: nearest preceding section marker
        header = i
        for j in range(i, max(-1, i - TOOL_WINDOW), -1):
            if RE_SECTION.match(lines[j]):
                header = j
                break
        # unload + re-feed group
        unload = None
        refeed_start = refeed_end = None
        disabled: list[int] = []
        end = m621
        for j in range(m621 + 1, window_end):
            st = _strip(lines[j])
            if RE_DISABLED.match(st):
                disabled.append(j)
                end = j
                continue
            if unload is None and RE_M620_11_UNLOAD.match(st):
                unload = j
                continue
            if unload is not None and refeed_start is None and RE_M620_11_FEED.match(st):
                # match the tail pattern group strictly
                k = j
                ok = True
                for pat in REFED_TAIL:
                    if k >= n or not pat.match(_strip(lines[k])):
                        ok = False
                        break
                    k += 1
                if ok:
                    refeed_start = j
                    refeed_end = k - 1
                    end = k - 1
                    break
            if RE_M620.match(st) and j != i:
                break
        if disabled:
            # a post-pause block: the disabled markers replace the unload/re-feed
            unload = disabled[0]
            refeed_start = disabled[-1] if len(disabled) > 1 else None
            refeed_end = disabled[-1] if len(disabled) > 1 else None
            end = disabled[-1]
        # z / temp
        z_line = m104_line = None
        for j in range(header, end + 1):
            st = _strip(lines[j])
            if z_line is None and RE_G1_Z.match(st):
                z_line = j
            if m104_line is None and RE_M104.match(st):
                m104_line = j

        # Skip the end-of-print unload pseudo change (T255 etc.).
        if tool >= 100:
            i = end + 1
            continue

        blocks.append(
            ToolChange(
                ordinal=ordinal,
                header_line=header,
                start_line=i,
                end_line=end,
                tool=tool,
                layer=layer_at.get(i, 1),
                demand_index=0,
                unload_line=unload,
                refeed_start=refeed_start,
                refeed_end=refeed_end,
                z_line=z_line,
                m104_line=m104_line,
            )
        )
        ordinal += 1
        i = end + 1

    # assign demand indices (collapse consecutive same-tool blocks)
    demand = -1
    prev = None
    for b in blocks:
        if b.tool != prev:
            demand += 1
            prev = b.tool
        b.demand_index = demand

    gf.tool_changes = blocks

    if not gf.filaments:
        gf.warnings.append("no '; filament_colour =' header found")
    if not blocks:
        gf.warnings.append("no AMS tool-change blocks found")
    return gf


def analysis_summary(gf: GcodeFile) -> dict:
    """A compact, JSON-friendly summary of a parsed file."""
    return {
        "filaments": gf.filaments,
        "filament_names": gf.colour_names(),
        "printer_model": gf.printer_model,
        "total_layers": len(gf.layers),
        "tool_changes": [
            {
                "ordinal": b.ordinal,
                "layer": b.layer,
                "tool": b.tool,
                "label": b.label,
                "demand_index": b.demand_index,
                "line": b.header_line + 1,
            }
            for b in gf.tool_changes
        ],
        "warnings": gf.warnings,
    }
