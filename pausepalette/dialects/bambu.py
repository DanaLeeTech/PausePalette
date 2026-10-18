"""Bambu Studio / OrcaSlicer AMS dialect.

Implements the validated manual spool-swap technique: pause immediately before
a tool-change block, unload the old filament back to the AMS, wait for the user
with ``M400 U1``, then continue into the tool-change block with its unload and
re-feed steps disabled because the filament was already unloaded at the pause.

The unload itself is the *printer's own*: every Bambu model ends a print by
pulling the filament back to the AMS, and that sequence differs per model — the
A1 cuts at X267, the X1/P1 park at X20 Y50 and wipe at X100, the newer
A2L/H2D/H2S/P2S/X2D unload with the ``T65535`` pseudo-tool. Those sequences are
vendored in :mod:`bambu_models` straight from Bambu Studio's printer profiles
and picked by the model named in the sliced file, so the pause moves are the
ones that machine actually uses.
"""

from __future__ import annotations

import re

from ..errors import StopAndExplain
from ..model import Pause, ToolChange
from .bambu_models import BAMBU_UNLOAD, unload_for

RE_UNLOAD = re.compile(r"^\s*M620\.11 S1 I\d+ E-\d")
RE_FEED = re.compile(r"^\s*M620\.11 S1 I\d+ E\d")
RE_ALREADY_DISABLED = re.compile(r"^\s*M620\.11 S0\b")
RE_REFED_TAIL = [
    RE_FEED,
    re.compile(r"^\s*M628 S1(?:\s*;.*)?$"),
    re.compile(r"^\s*G92 E0(?:\s*;.*)?$"),
    re.compile(r"^\s*G1 E[\d.]+"),
    re.compile(r"^\s*M400(?:\s*;.*)?$"),
    re.compile(r"^\s*M629 S1(?:\s*;.*)?$"),
]

DISABLED_LINE = "M620.11 S0 ; filament already unloaded at pause"

#: the printer whose unload we fall back to when the file names an unknown one
FALLBACK_MODEL = "A1"


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def build_pause_block(
    pause: Pause,
    total: int,
    newline: str,
    z: str,
    feed: str,
    temp: int | None,
    printer_model: str | None = None,
) -> list[str]:
    """Return the pause block lines (each with its line ending).

    ``printer_model`` selects the unload sequence; an unknown model falls back
    to the A1's and the caller should warn.
    """
    unload = unload_for(printer_model) or BAMBU_UNLOAD[FALLBACK_MODEL]

    body = [
        "M1007 S0 ; turn off mass estimation",
        "G392 S0",
        unload[0],
        "M204 S9000",
        f"G1 Z{z} F{feed}",
        "M400",
        "M106 P1 S0",
        "M106 P2 S0",
        f"M104 S{temp if temp is not None else ''}".rstrip(),
        "",
        *unload[1:],
        "M400",
        "M400 U1 ; PAUSE: swap spools now, then resume",
    ]
    lines = [
        f"; ===== MANUAL SPOOL SWAP {pause.index} of {total} =====",
        *[f"; swap: {s.describe()}" for s in pause.swaps],
        *body,
        f"; ===== END MANUAL SPOOL SWAP {pause.index} =====",
    ]
    return [line + newline for line in lines]


def unload_marker(printer_model: str | None) -> str:
    """The pseudo-tool the unload uses: ``T255``, ``T65535``, ... .

    Used to verify that a pause block really unloads, per printer.
    """
    unload = unload_for(printer_model) or BAMBU_UNLOAD[FALLBACK_MODEL]
    for line in unload:
        if line.startswith("T") and line[1:].split()[0].isdigit():
            return line.split()[0]
    return "T255"  # pragma: no cover - every vendored entry has one


def transform_following_block(
    lines: list[str],
    block: ToolChange,
    newline: str,
) -> tuple[list[str], int, int]:
    """Apply the step-D edits to the block that follows a pause.

    Returns (new_lines, lines_removed, lines_changed). Raises StopAndExplain if
    the block has neither the unload / re-feed structure nor the slicer's own
    "already disabled" form.
    """
    stripped = [ln.strip() for ln in lines]
    out = list(lines)
    removed = 0
    changed = 0
    already = any(RE_ALREADY_DISABLED.match(st) for st in stripped)

    # 1. the unload line -> disabled
    unload_idx = None
    for i, st in enumerate(stripped):
        if RE_UNLOAD.match(st):
            unload_idx = i
            break
    if unload_idx is not None:
        indent = _indent(out[unload_idx])
        out[unload_idx] = indent + DISABLED_LINE + newline
        changed += 1
        search_from = unload_idx + 1
    elif already:
        # this filament has no long retraction when cut, so the slicer already
        # wrote `M620.11 S0` here
        search_from = 0
    else:
        raise StopAndExplain(
            f"T{block.tool} block: expected an unload line "
            f"'M620.11 S1 I<n> E-<dist> F...' or a disabled 'M620.11 S0' "
            f"but neither was found"
        )

    # 2. the re-feed group -> one disabled line
    feed_idx = None
    for i in range(search_from, len(stripped)):
        if RE_FEED.match(stripped[i]):
            feed_idx = i
            break
    if feed_idx is None:
        if already:
            return out, removed, changed
        raise StopAndExplain(
            f"T{block.tool} block: expected the 6-line re-feed group after the "
            f"unload line but it was not found"
        )
    if feed_idx + len(RE_REFED_TAIL) > len(stripped):
        raise StopAndExplain(f"T{block.tool} block: truncated re-feed group")
    for j, pat in enumerate(RE_REFED_TAIL):
        if not pat.match(stripped[feed_idx + j]):
            raise StopAndExplain(
                f"T{block.tool} block: re-feed group differs at line "
                f"{feed_idx + j + 1} (expected {j + 1} of 6)"
            )
    end = feed_idx + len(RE_REFED_TAIL)
    removed += end - feed_idx - 1  # net line removal (one line replaces the group)
    indent = _indent(out[feed_idx])
    out[feed_idx:end] = [indent + DISABLED_LINE + newline]
    return out, removed, changed
