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
from pausepalette.pipeline import (  # noqa: E402
    _sequence_from_layers,
    insert_into_slice,
    plan_from_project,
)
from pausepalette import threemf  # noqa: E402

REAL = os.environ.get("PAUSEPALETTE_REAL_3MF") or "/home/dana/Pictures/cfs peeps/keyrings.gcode.3mf"
REAL_UNSLICED = (
    os.environ.get("PAUSEPALETTE_REAL_UNSLICED_3MF")
    or "/home/dana/Pictures/cfs peeps/before pauses.3mf"
)

needs_real = pytest.mark.skipif(
    not Path(REAL).exists(), reason="set PAUSEPALETTE_REAL_3MF to a real Bambu project"
)
needs_real_unsliced = pytest.mark.skipif(
    not Path(REAL_UNSLICED).exists(),
    reason="set PAUSEPALETTE_REAL_UNSLICED_3MF to a real unsliced Bambu project",
)


@pytest.fixture(scope="module")
def real_data() -> bytes:
    return Path(REAL).read_bytes()


@pytest.fixture(scope="module")
def real_unsliced() -> bytes:
    return Path(REAL_UNSLICED).read_bytes()


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


@needs_real
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


@needs_real
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


@needs_real
def test_real_project_keeps_the_darkest_colour_resident(real_data):
    """The darkest filament is the spool that never has to be swapped."""
    from pausepalette import colours as C

    stage1 = plan_from_project(real_data, slots=4, plate=1)
    plan = stage1.plan
    lums = {i: C.luminance(h) for i, h in enumerate(plan.filament_colour)}
    darkest = min(lums, key=lambda t: lums[t])

    swapped_out = {s.out_tool for p in plan.pauses for s in p.swaps}
    assert darkest not in swapped_out, (
        f"{plan.filament_name[darkest]} is the darkest colour and should stay put"
    )
    # slot 1 holds it, and the reordered project's T0 is slot 1
    assert plan.initial[0] == darkest
    assert plan.colour_moves == 0


def _part_colours(data: bytes, base: int = 1) -> dict[str, str]:
    """part uuid -> the colour hex it prints with, from its own filament ref."""
    entries = threemf._read_zip(data)
    colours = json.loads(entries["Metadata/project_settings.config"])["filament_colour"]
    xml = entries["Metadata/model_settings.config"].decode("utf-8", errors="replace")
    out: dict[str, str] = {}
    for m in re.finditer(r'<part [^>]*uuid="([^"]+)"[^>]*>(.*?)</part>', xml, re.S):
        ref = re.search(r'key="(?:filament_id|extruder)" value="(\d+)"', m.group(2))
        if ref:
            out[m.group(1)] = colours[int(ref.group(1)) - base].upper()
    return out


@needs_real_unsliced
def test_real_unsliced_reorder_keeps_every_parts_colour(real_unsliced):
    """A per-part-coloured project must survive the reorder artwork-identical.

    Bambu writes the per-part refs 1-based; reading them as 0-based shifted every
    reference by one tool and recoloured the model (414 of 638 parts here).
    """
    stage1 = plan_from_project(real_unsliced, slots=4, plate=1)
    # the reorder must actually change the order, or this proves nothing
    assert stage1.plan.filament_colour != stage1.original_filaments

    before = _part_colours(real_unsliced)
    after = _part_colours(stage1.reordered_project)
    assert len(before) > 100          # the real keychain has hundreds of parts
    assert after == before            # every part keeps the colour it had
    assert stage1.reorder_report.references_remapped >= len(before)


@needs_real_unsliced
@needs_real
def test_real_unsliced_sequence_matches_the_real_slice(real_unsliced, real_data):
    """The model's geometry recovers exactly the sequence the slicer produces."""
    info = threemf.read_project(real_unsliced, plate=1)
    geometry = threemf.filament_layers(real_unsliced, info.plate_by_id(1), info.layer_height)
    assert geometry is not None
    by_colour, last_layer = geometry
    # the artwork: all nine colours on layers 1-3, black throughout, white to
    # layer 6, dark grey again near the top
    assert last_layer == 25
    assert by_colour[1] == set(range(1, 26))
    assert by_colour[9] == {1, 2, 3, 4, 5, 6}
    assert by_colour[6] == {1, 2, 3, 22, 23, 24, 25}
    assert by_colour[2] == {1, 2, 3}

    used: dict[int, set[int]] = {}
    for colour, where in by_colour.items():
        for layer in where:
            used.setdefault(layer, set()).add(colour)
    tools, layers = _sequence_from_layers(info.plate_by_id(1), used, last_layer)

    slice_info = threemf.read_project(real_data, plate=1)
    gf = parse_gcode(slice_info.slice_gcode)
    assert [t + 1 for t in tools] == [b.tool + 1 for b in gf.tool_changes]
    assert layers == [b.layer for b in gf.tool_changes]


@needs_real_unsliced
def test_real_unsliced_project_gets_the_whole_plan(real_unsliced):
    """The unsliced project yields the full plan — six pauses, two per layer."""
    stage1 = plan_from_project(real_unsliced, slots=4, plate=1)
    plan = stage1.plan
    assert plan.provisional
    assert not plan.deferred
    assert len(plan.pauses) == 6            # the fewest possible for this print
    assert plan.swaps_total <= 20
    assert [p.layer for p in plan.pauses] == [1, 1, 2, 2, 3, 3]
    assert any("geometry" in n for n in stage1.notes)
    assert plan.expected_demands == []
    # the darkest colour is printed first and the order is written into the project
    assert plan.first_colour == 1
    assert stage1.reorder_report.sequence_written
    assert max(plan.layer_orders) == 25


@needs_real_unsliced
def test_real_unsliced_plan_can_start_on_another_colour(real_unsliced):
    stage1 = plan_from_project(real_unsliced, slots=4, plate=1, first_colour=5)
    plan = stage1.plan
    assert plan.first_colour == 5
    assert plan.layer_orders[min(plan.layer_orders)][0] == 5
    assert len(plan.pauses) >= 1


@needs_real_unsliced
@needs_real
def test_real_unsliced_provisional_plan_finalises_from_the_real_slice(real_unsliced, real_data):
    """Re-slice the reordered project and upload it: the provisional plan becomes exact."""
    plan = plan_from_project(real_unsliced, slots=4, plate=1).plan
    info = threemf.read_project(real_data, plate=1)
    resliced = _reslice(info.slice_gcode, plan)
    result = insert_into_slice(resliced.encode("utf-8"), plan)
    assert result.report.ok, result.report.failures()
    assert result.plan is not None
    assert not result.plan.deferred and not result.plan.provisional
    assert len(result.plan.pauses) >= len(plan.pauses)
