"""Insertion of pause blocks into a sliced G-code file."""

from __future__ import annotations

import re

from . import dialects
from .dialects import bambu, generic
from .errors import StopAndExplain
from .gcode import detect_newline
from .model import GcodeFile, InsertionResult, Plan

RE_G1_Z = re.compile(r"^G1 Z([\d.]+) F(\d+)")
RE_M104 = re.compile(r"^M104 S(\d+)")


def _extract_z_feed_temp(
    gf: GcodeFile, block, plan: Plan
) -> tuple[str, str, int | None]:
    z, feed, temp = "0.4", "1200", None
    if block.z_line is not None:
        m = RE_G1_Z.match(gf.lines[block.z_line].strip())
        if m:
            z, feed = m.group(1), m.group(2)
    if block.m104_line is not None:
        m = RE_M104.match(gf.lines[block.m104_line].strip())
        if m:
            temp = int(m.group(1))
    if temp is None:
        plan.warnings.append(
            f"T{block.tool} block: no 'M104 S...' found; pause omits the nozzle-temperature line"
        )
    return z, feed, temp


def insert_pauses(
    gf: GcodeFile,
    plan: Plan,
    newline: str | None = None,
) -> InsertionResult:
    """Insert the plan's pause blocks and apply the step-D edits.

    The original file is never modified; a new text is returned. ``newline``
    defaults to the ending the file already uses (CRLF or LF), so the export
    keeps one style throughout.
    """
    if newline is None:
        newline = detect_newline(gf.text())
    result = InsertionResult(text="", inserted_blocks=0, edited_blocks=0,
                             lines_inserted=0, lines_removed=0, lines_changed=0)
    if not plan.pauses:
        result.text = gf.text()
        return result

    by_ordinal = {b.ordinal: b for b in gf.tool_changes}
    anchors = {p.anchor_ordinal: p for p in plan.pauses}
    total = len(plan.pauses)

    # Bambu slices get the cut-and-retract pause and the block that follows it
    # rewritten; every other machine keeps its own change sequence and only gets
    # the pause written in front of it. That is the *only* place a printer is
    # consulted — the plan was made without looking at it.
    dialect = dialects.for_file(gf)

    if dialect == "bambu" and bambu.unload_for(gf.printer_model) is None:
        plan.warnings.append(
            f"printer {gf.printer_model or 'unknown'!r} is not one of the printers "
            f"Bambu Studio ships a profile for; the {bambu.FALLBACK_MODEL} unload "
            f"sequence was used, so check the pause moves"
        )
    elif dialect == "generic" and generic.is_blunt(gf.printer_model):
        plan.warnings.append(
            f"this printer pauses with {generic.pause_command(gf.printer_model)!r}, "
            f"which does not park or retract the head on its own — check that it "
            f"stops somewhere safe before printing"
        )

    starts_at: dict[int, object] = {b.header_line: b for b in gf.tool_changes}

    out: list[str] = []
    i = 0
    n = len(gf.lines)
    while i < n:
        block = starts_at.get(i)
        if block is not None and block.ordinal in anchors:
            pause = anchors[block.ordinal]
            if pause.tool != block.tool:
                raise StopAndExplain(
                    f"pause {pause.index}: plan expects T{pause.tool} at block "
                    f"{block.ordinal} but the file has T{block.tool}"
                )
            if dialect == "bambu":
                z, feed, temp = _extract_z_feed_temp(gf, block, plan)
                pause_lines = dialects.build_pause_block(
                    pause, total, newline, gf, z=z, feed=feed, temp=temp
                )
            else:
                pause_lines = dialects.build_pause_block(pause, total, newline, gf)
            out.extend(pause_lines)
            result.changes.append((i, i, pause_lines))
            result.lines_inserted += len(pause_lines)
            result.inserted_blocks += 1

            block_lines = gf.lines[block.header_line : block.end_line + 1]
            if dialect == "bambu":
                new_block, removed, changed = bambu.transform_following_block(
                    block_lines, block, newline
                )
                out.extend(new_block)
                result.changes.append((block.header_line, block.end_line + 1, new_block))
                result.lines_removed += removed
                result.lines_changed += changed
                result.edited_blocks += 1
            else:
                out.extend(block_lines)
            i = block.end_line + 1
            continue
        out.append(gf.lines[i])
        i += 1

    result.text = "".join(out)
    result.warnings = list(plan.warnings)
    return result
