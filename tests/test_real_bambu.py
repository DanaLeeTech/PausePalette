"""Regression tests against a real Bambu Studio project.

The 24 MB plate G-code that ships in a real project uses a very different block
shape from the small fixtures (M621 sits hundreds of lines below M620, T lines
can be indented, some blocks already carry `M620.11 S0`). Point
``PAUSEPALETTE_REAL_3MF`` at such a file to run these; they are skipped
otherwise.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pausepalette.gcode import parse_gcode  # noqa: E402
from pausepalette.pipeline import insert_into_slice, plan_from_project  # noqa: E402
from pausepalette import threemf  # noqa: E402

REAL = os.environ.get("PAUSEPALETTE_REAL_3MF") or "/home/dana/Pictures/cfs peeps/keyrings.gcode.3mf"

pytestmark = pytest.mark.skipif(
    not Path(REAL).exists(), reason="set PAUSEPALETTE_REAL_3MF to a real Bambu project"
)


@pytest.fixture(scope="module")
def real_data() -> bytes:
    return Path(REAL).read_bytes()


def _reslice(gcode: str, plan) -> str:
    """Simulate Bambu re-slicing after the filament list was reordered."""
    old_to_new = {}
    for new, hexv in enumerate(plan.filament_colour):
        old_to_new[plan.original_filaments.index(hexv)] = new
    gf = parse_gcode(gcode)
    lines = gcode.splitlines(keepends=True)
    for b in gf.tool_changes:
        n = old_to_new[b.tool]
        lines[b.start_line] = re.sub(r"M620 S\d+A", f"M620 S{n}A", lines[b.start_line])
        lines[b.tool_line] = re.sub(r"T\d+", f"T{n}", lines[b.tool_line])
        if b.unload_line is not None:
            lines[b.unload_line] = re.sub(
                r"M620\.11 S1 I\d+", f"M620.11 S1 I{n}", lines[b.unload_line]
            )
    for i, ln in enumerate(lines):
        if ln.startswith("; filament_colour ="):
            lines[i] = "; filament_colour = " + ";".join(plan.filament_colour) + "\n"
            break
    return "".join(lines)


def test_real_project_parses_like_bambu_says(real_data):
    info = threemf.read_project(real_data, plate=1)
    assert info.ok
    assert len(info.plates) >= 1
    assert info.has_slice
    gf = parse_gcode(info.slice_gcode)
    assert len(gf.tool_changes) == len(info.plate_by_id(1).sequence)
    # the parser's sequence must equal Bambu's own stored sequence (1-based)
    assert [b.tool + 1 for b in gf.tool_changes] == info.plate_by_id(1).sequence
    assert all(gf.lines[b.header_line].startswith(";=====") for b in gf.tool_changes)
    # both block flavours are present in real files
    assert any(b.unload_line is not None for b in gf.tool_changes)


def test_real_project_full_flow(real_data):
    stage1 = plan_from_project(real_data, slots=4, plate=1)
    plan = stage1.plan
    assert not plan.provisional and not plan.deferred
    assert len(plan.pauses) >= 1
    assert plan.filament_colour and len(plan.filament_colour) == len(stage1.original_filaments)

    info = threemf.read_project(real_data, plate=1)
    resliced = _reslice(info.slice_gcode, plan)
    result = insert_into_slice(resliced.encode("utf-8"), plan)
    assert result.report.ok, result.report.failures()
    edited = result.edited.text
    assert edited.count("; ===== MANUAL SPOOL SWAP ") == len(plan.pauses)
    assert edited.count("; ===== END MANUAL SPOOL SWAP ") == len(plan.pauses)
    u1 = [
        ln
        for ln in edited.splitlines()
        if ln.strip().startswith("M400 U1")
    ]
    assert len(u1) == len(plan.pauses)
    # the original is untouched
    assert "MANUAL SPOOL SWAP" not in info.slice_gcode
