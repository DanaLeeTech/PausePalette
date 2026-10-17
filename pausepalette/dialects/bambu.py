"""Bambu Studio / OrcaSlicer AMS dialect.

Implements the validated manual spool-swap technique: pause immediately before
a tool-change block, cut and retract via ``T255`` (the same sequence the printer
uses at the end of a print), wait for the user with ``M400 U1``, then continue
into the tool-change block with its unload and re-feed steps disabled because
the filament was already unloaded at the pause.
"""

from __future__ import annotations

import re

from ..errors import StopAndExplain
from ..model import Pause, ToolChange

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


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def build_pause_block(
    pause: Pause,
    total: int,
    newline: str,
    z: str,
    feed: str,
    temp: int | None,
) -> list[str]:
    """Return the pause block lines (each with its line ending)."""
    swap_lines = [f"; swap: {s.describe()}" for s in pause.swaps]
    body = [
        f"; ===== MANUAL SPOOL SWAP {pause.index} of {total} =====",
        *swap_lines,
        "M1007 S0 ; turn off mass estimation",
        "G392 S0",
        "M620 S255",
        "M204 S9000",
        f"G1 Z{z} F{feed}",
        "M400",
        "M106 P1 S0",
        "M106 P2 S0",
        f"M104 S{temp if temp is not None else ''}".rstrip(),
        "",
        "G1 X267 F18000 ; cutter position, same as automatic colour change",
        "M400",
        "T255 ; cut and retract filament back to AMS, same as end of print",
        "G1 X-28.5 F18000",
        "G1 X-48.2 F3000",
        "M621 S255",
        "M400",
        "M400 U1 ; PAUSE: swap spools now, then resume",
        f"; ===== END MANUAL SPOOL SWAP {pause.index} =====",
    ]
    return [line + newline for line in body]


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
