"""Independent verification of an edited G-code file."""

from __future__ import annotations

import re

from .gcode import parse_gcode
from .model import GcodeFile, InsertionResult, Plan, VerificationReport

RE_M400U1 = re.compile(r"^M400 U1\b")
RE_SWAP_START = re.compile(r"^; ===== MANUAL SPOOL SWAP (\d+) of (\d+) =====")
RE_SWAP_END = re.compile(r"^; ===== END MANUAL SPOOL SWAP (\d+) =====")
RE_DISABLED_FORM = re.compile(r"^M620\.11 S0\b")
DISABLED = "M620.11 S0 ; filament already unloaded at pause"


def _rebuild(original: GcodeFile, result: InsertionResult) -> str:
    """Rebuild the expected output from the record of changes."""
    out: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(result.changes, key=lambda c: c[0]):
        out.extend(original.lines[cursor:start])
        out.extend(replacement)
        cursor = end
    out.extend(original.lines[cursor:])
    return "".join(out)


def verify(original: GcodeFile, result: InsertionResult, plan: Plan) -> VerificationReport:
    report = VerificationReport(ok=True)
    edited_text = result.text
    edited = parse_gcode(edited_text)

    # 1. every byte outside the recorded changes is identical
    expected = _rebuild(original, result)
    report.add(
        "byte-identical outside recorded changes",
        expected == edited_text,
        "" if expected == edited_text else "reconstructed text differs from output",
    )

    # 2. tool-change sequence preserved exactly
    before = [(b.tool, b.layer) for b in original.tool_changes]
    after = [(b.tool, b.layer) for b in edited.tool_changes]
    report.add(
        "tool-change sequence unchanged",
        before == after,
        f"{len(before)} blocks before, {len(after)} after" if before != after else f"{len(before)} blocks",
    )

    # 3. exactly one *new* M400 U1 per pause (the file may already have one at
    #    the end of the print)
    before_u1 = sum(1 for ln in original.lines if RE_M400U1.match(ln.strip()))
    u1 = [i for i, ln in enumerate(edited.lines) if RE_M400U1.match(ln.strip())]
    report.add(
        "exactly one new M400 U1 per pause",
        len(u1) == before_u1 + len(plan.pauses),
        f"{len(u1)} found, {before_u1 + len(plan.pauses)} expected "
        f"({before_u1} pre-existing)",
    )

    # 4. each pause block sits immediately before its anchor block, with one T255
    ok_anchor = True
    detail = []
    by_ordinal = {b.ordinal: b for b in edited.tool_changes}
    for pause in plan.pauses:
        block = by_ordinal.get(pause.anchor_ordinal)
        if block is None:
            ok_anchor = False
            detail.append(f"pause {pause.index}: anchor block missing")
            continue
        hdr = block.header_line
        prev = edited.lines[hdr - 1].strip() if hdr > 0 else ""
        m = RE_SWAP_END.match(prev)
        if not m or int(m.group(1)) != pause.index:
            ok_anchor = False
            detail.append(f"pause {pause.index}: not immediately before T{block.tool}")
        # find the matching start marker above
        start = None
        for j in range(hdr - 1, max(-1, hdr - 60), -1):
            ms = RE_SWAP_START.match(edited.lines[j].strip())
            if ms and int(ms.group(1)) == pause.index:
                start = j
                break
        if start is None:
            ok_anchor = False
            detail.append(f"pause {pause.index}: start marker missing")
        else:
            body = [ln.strip() for ln in edited.lines[start:hdr]]
            if sum(1 for ln in body if ln.startswith("T255")) != 1:
                ok_anchor = False
                detail.append(f"pause {pause.index}: expected one T255")
            if block.layer != pause.layer or block.tool != pause.tool:
                ok_anchor = False
                detail.append(
                    f"pause {pause.index}: layer/tool {block.layer}/T{block.tool} "
                    f"!= plan {pause.layer}/T{pause.tool}"
                )
    report.add("pause blocks anchored before the right tool change", ok_anchor, "; ".join(detail))

    # 5. unload and re-feed disabled in each anchored block (either our marker or
    #    the slicer's own 'M620.11 S0' for filaments with no long retraction)
    ok_d = True
    detail = []
    for pause in plan.pauses:
        block = by_ordinal.get(pause.anchor_ordinal)
        if block is None:
            ok_d = False
            continue
        span = edited.lines[block.header_line : block.end_line + 1]
        count = sum(1 for ln in span if RE_DISABLED_FORM.match(ln.strip()))
        if count != 2:
            ok_d = False
            detail.append(f"T{block.tool}: {count} disabled lines, expected 2")
    report.add("unload and re-feed disabled in the following block", ok_d, "; ".join(detail))

    return report
