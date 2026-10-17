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
RE_M620 = re.compile(r"^\s*M620 S(\d+)A\s*(?:;.*)?$")
RE_M621 = re.compile(r"^\s*M621 S(\d+)A\s*(?:;.*)?$")
RE_T_COMMAND = re.compile(r"^\s*T(\d+)\s*(?:;.*)?$")
RE_M620_11_UNLOAD = re.compile(r"^\s*M620\.11 S1 I(\d+) E-\d")
RE_M620_11_FEED = re.compile(r"^\s*M620\.11 S1 I(\d+) E\d")
RE_M629 = re.compile(r"^\s*M629 S1\s*(?:;.*)?$")
RE_DISABLED = re.compile(r"^\s*M620\.11 S0(?:\s*;.*)?$")
RE_G1_Z = re.compile(r"^G1 Z([\d.]+) F(\d+)")
RE_M104 = re.compile(r"^M104 S(\d+)")

# The re-feed group that follows an unload line (Bambu/Orca shape).
REFED_TAIL = [
    re.compile(r"^\s*M620\.11 S1 I\d+ E\d"),
    re.compile(r"^\s*M628 S1\s*(?:;.*)?$"),
    re.compile(r"^\s*G92 E0\s*(?:;.*)?$"),
    re.compile(r"^\s*G1 E[\d.]+"),
    re.compile(r"^\s*M400\s*(?:;.*)?$"),
    re.compile(r"^\s*M629 S1\s*(?:;.*)?$"),
]

# A real Bambu tool-change block runs from M620 to M621 across the whole flush
# and wipe section (hundreds of lines). The pieces we edit are always near the
# start, so a bounded window — also clamped to the next block — is enough and
# keeps parsing linear.
BLOCK_WINDOW = 400
HEADER_LOOKBACK = 20


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
    saw_explicit = False
    markers: list[LayerMarker] = []
    for i, raw in enumerate(lines):
        if not raw.startswith(";"):
            continue
        line = raw.strip()
        m = RE_LAYER_NUM_TOTAL.match(line)
        if m:
            layer = int(m.group(1))
            total_layers = int(m.group(2))
            saw_explicit = True
            markers.append(LayerMarker(line=i, layer=layer, total=total_layers))
            continue
        m = RE_LAYER_NUM.match(line)
        if m:
            layer = int(m.group(1))
            saw_explicit = True
            markers.append(LayerMarker(line=i, layer=layer, total=None))
            continue
        if RE_LAYER_CHANGE.match(line):
            # ignore stray layer-change markers in the start gcode (before the
            # first explicit layer number)
            if saw_explicit:
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
    # A real Bambu block runs from `M620 S<n>A` through the flush/wipe section to
    # `M621 S<n>A`, which can be hundreds of lines later. We locate the block by
    # its M620 line, take the first T command for the tool, and pick up the
    # unload / re-feed pieces near the start. Blocks whose filament has no
    # "long retractions when cut" carry `M620.11 S0` instead and are recorded as
    # already disabled.
    blocks: list[ToolChange] = []
    ordinal = 0
    i = 0
    n = len(lines)
    while i < n:
        if not RE_M620.match(lines[i]):
            i += 1
            continue
        window_end = min(n, i + BLOCK_WINDOW)
        for j in range(i + 1, window_end):
            if RE_M620.match(lines[j]):
                window_end = j
                break

        tool = None
        tool_line = None
        m621 = None
        for j in range(i + 1, window_end):
            st = lines[j]
            if tool is None and RE_T_COMMAND.match(st):
                tool = int(RE_T_COMMAND.match(st).group(1))
                tool_line = j
            if RE_M621.match(st):
                m621 = j
                break
        if tool is None:
            i += 1
            continue

        header = i
        for j in range(i, max(-1, i - HEADER_LOOKBACK), -1):
            if RE_SECTION.match(lines[j]):
                header = j
                break

        unload = None
        refeed_start = refeed_end = None
        disabled: list[int] = []
        end = m621 if m621 is not None else i
        for j in range(i + 1, window_end):
            st = lines[j]
            if RE_DISABLED.match(st):
                disabled.append(j)
                end = max(end, j)
                continue
            if unload is None and RE_M620_11_UNLOAD.match(st):
                unload = j
                end = max(end, j)
                continue
            if unload is not None and refeed_start is None and RE_M620_11_FEED.match(st):
                k = j
                ok = True
                for pat in REFED_TAIL:
                    if k >= window_end or not pat.match(lines[k]):
                        ok = False
                        break
                    k += 1
                if ok:
                    refeed_start = j
                    refeed_end = k - 1
                    end = max(end, k - 1)
                break
        if disabled and unload is None:
            # already in the disabled form (no long retraction when cut)
            refeed_start = disabled[-1] if len(disabled) > 1 else None
            refeed_end = disabled[-1] if len(disabled) > 1 else None

        z_line = m104_line = None
        for j in range(header, end + 1):
            st = _strip(lines[j])
            if z_line is None and RE_G1_Z.match(st):
                z_line = j
            if m104_line is None and RE_M104.match(st):
                m104_line = j

        # Skip pseudo tools: T255 (end-of-print unload) and T1000 (no tool).
        if tool >= 100:
            i = max(end, i) + 1
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
                tool_line=tool_line,
                unload_line=unload,
                refeed_start=refeed_start,
                refeed_end=refeed_end,
                z_line=z_line,
                m104_line=m104_line,
            )
        )
        ordinal += 1
        i = max(end, i) + 1

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
