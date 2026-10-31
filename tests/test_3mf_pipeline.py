from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pausepalette import threemf  # noqa: E402
from pausepalette.errors import StopAndExplain  # noqa: E402
from pausepalette.gcode import parse_gcode  # noqa: E402
from pausepalette.pipeline import (  # noqa: E402
    insert_into_slice,
    plan_and_insert,
    plan_from_dict,
    plan_from_project,
    plan_to_dict,
)
from tests.fixtures import FILAMENTS, keychain_gcode  # noqa: E402
from tests.make_3mf import build_3mf, geometry_3mf, project_settings  # noqa: E402


def _part_colours(data: bytes) -> dict[str, str]:
    """part id -> the colour hex it prints with (via its own filament ref)."""
    entries = threemf._read_zip(data)
    colours = json.loads(entries["Metadata/project_settings.config"])["filament_colour"]
    xml = entries["Metadata/model_settings.config"].decode()
    out: dict[str, str] = {}
    for m in re.finditer(r'<part id="(\d+)"[^>]*>(.*?)</part>', xml, re.S):
        ref = re.search(r'key="(?:filament_id|extruder)" value="(\d+)"', m.group(2))
        if ref and int(ref.group(1)) != 0:  # 0 is "default", not a colour of its own
            out[m.group(1)] = colours[int(ref.group(1)) - 1].upper()
    return out


def test_read_project():
    data = build_3mf()
    info = threemf.read_project(data)
    assert info.ok
    assert info.filaments == FILAMENTS
    assert info.printer_model == "Bambu Lab A1"
    assert info.has_slice
    assert info.reference_count == 2 * len(FILAMENTS)  # filament_id + extruder per part
    assert "Metadata/model_settings.config" in info.entries


def test_reorder_keeps_artwork():
    data = build_3mf()
    order = [0, 5, 2, 1, 4, 6, 7, 3, 8]
    new_data, report = threemf.reorder_project(data, order)

    settings = json.loads(threemf._read_zip(new_data)["Metadata/project_settings.config"])
    assert settings["filament_colour"] == [FILAMENTS[i] for i in order]
    # per-filament lists follow their filament
    assert settings["filament_vendor"] == [f"Vendor{old}" for old in order]
    # denylisted per-printer list untouched
    assert report.json_keys_skipped == ["printer_fake_per_printer"]
    assert settings["printer_fake_per_printer"] == [f"p{i}" for i in range(9)]

    # model_settings references remapped: part p (old filament p, 1-based) now
    # points at the new index of that same colour
    xml = threemf._read_zip(new_data)["Metadata/model_settings.config"].decode()
    old_to_new = {old: new for new, old in enumerate(order)}
    for old in range(9):
        assert f'key="filament_id" value="{old_to_new[old] + 1}"' in xml

    # and every part keeps the colour it printed with before the reorder
    assert _part_colours(new_data) == _part_colours(data)
    assert report.references_remapped == 2 * len(FILAMENTS)

    # stale slice stripped, everything else preserved
    assert "Metadata/plate_1.gcode" not in threemf._read_zip(new_data)
    assert report.slices_stripped


def test_reorder_keeps_artwork_with_default_filament_refs():
    """0 means "default filament" (negative parts), not filament 0.

    Reading it as 0-based shifted every other reference by one tool and
    recoloured the model — the magnets project hit exactly this.
    """
    data = build_3mf(default_refs=True)
    order = [0, 5, 2, 1, 4, 6, 7, 3, 8]
    new_data, report = threemf.reorder_project(data, order)

    assert _part_colours(new_data) == _part_colours(data)
    xml = threemf._read_zip(new_data)["Metadata/model_settings.config"].decode()
    assert xml.count('key="filament_id" value="0"') == 1  # still the default
    assert xml.count('key="extruder" value="0"') == 1
    assert report.references_remapped == 2 * len(FILAMENTS)
    assert not report.warnings


def test_reorder_detects_bad_permutation():
    with pytest.raises(StopAndExplain):
        threemf.reorder_project(build_3mf(), [0, 1, 2])


def test_inconsistent_project_and_slice_stop_and_explain():
    # the project lists 9 filaments but the embedded slice names 3
    data = build_3mf(gcode=keychain_gcode(filaments=FILAMENTS[:3]))
    with pytest.raises(StopAndExplain) as excinfo:
        plan_from_project(data, slots=4)
    assert "inconsistent" in str(excinfo.value)


def test_unsliced_project_is_accepted():
    project = build_3mf(sliced=False)
    stage1 = plan_from_project(project, slots=4)
    plan = stage1.plan
    assert plan.provisional
    assert not plan.deferred
    assert plan.pauses  # 9 colours in 4 slots, from the first-layer order
    assert len(plan.filament_colour) == 9
    # a reordered project is still produced (with the stale slice absent anyway)
    assert stage1.reordered_project[:2] == b"PK"
    assert any("not sliced" in n for n in stage1.notes)


def test_geometry_says_which_layers_each_colour_prints():
    """An unsliced project's parts carry the per-layer colour use."""
    project = geometry_3mf()
    info = threemf.read_project(project, plate=1)
    got = threemf.filament_layers(project, info.plate_by_id(1), info.layer_height)
    assert got is not None
    by_colour, last_layer = got
    assert last_layer == 25
    assert by_colour[1] == set(range(1, 26))          # black runs the whole way up
    assert by_colour[9] == {1, 2, 3, 4, 5, 6}          # white the first six layers
    assert by_colour[6] == {1, 2, 3, 22, 23, 24, 25}   # dark grey, image and top
    assert by_colour[4] == {1, 2, 3}                   # an image colour


def test_unsliced_geometry_plan_covers_the_whole_print():
    """The plan is worked out from the model, so it is the whole print, not a start."""
    project = geometry_3mf()
    stage1 = plan_from_project(project, slots=4)
    plan = stage1.plan
    assert plan.provisional
    assert not plan.deferred
    # the reference print's six pauses — two each in the first three layers
    assert len(plan.pauses) == 6
    assert plan.swaps_total <= 20
    assert [p.layer for p in plan.pauses] == [1, 1, 2, 2, 3, 3]
    # the later layers (dark grey returning near the top) are part of the plan
    assert max(plan.block_layers) > 3
    assert any("geometry" in n for n in stage1.notes)


def test_first_colour_defaults_to_the_darkest_and_can_be_chosen():
    project = geometry_3mf()
    default = plan_from_project(project, slots=4).plan
    assert default.first_colour == 1          # black is the darkest colour
    assert default.first_colour_choices == list(range(1, 10))
    assert default.first_layer_order == []    # nothing pinned: the order is chosen
    assert default.layer_orders[min(default.layer_orders)][0] == 1

    other = plan_from_project(project, slots=4, first_layer_order=[5]).plan
    assert other.first_colour == 5
    assert other.layer_orders[min(other.layer_orders)][0] == 5
    assert other.first_layer_order == [5]
    assert len(other.pauses) >= 1
    assert other.first_colour_choices == list(range(1, 10))


def test_first_layer_order_can_be_pinned_position_by_position():
    """Choosing the 2nd, 3rd, ... colour fixes those spots; the rest stay free."""
    project = geometry_3mf()
    plan = plan_from_project(project, slots=4, first_layer_order=[1, 9, None, 4]).plan
    assert plan.first_colour == 1
    first = plan.layer_orders[min(plan.layer_orders)]
    assert first[:2] == [1, 9] and first[3] == 4   # the pins, in their places
    assert sorted(first) == list(range(1, 10))     # every layer-1 colour once
    assert plan.first_layer_order == [1, 9, None, 4]  # echoed, so the UI keeps them


def test_first_layer_order_can_be_chosen_completely():
    project = geometry_3mf()
    order = [3, 1, 9, 2, 8, 4, 7, 5, 6]
    plan = plan_from_project(project, slots=4, first_layer_order=order).plan
    assert plan.layer_orders[min(plan.layer_orders)] == order
    assert plan.first_colour == 3
    assert plan.first_layer_order == order
    assert plan_from_dict(json.loads(json.dumps(plan_to_dict(plan)))).first_layer_order == order


def test_bad_first_layer_pins_are_ignored():
    project = geometry_3mf()
    plan = plan_from_project(project, slots=4, first_layer_order=[1, 99, 1, 7]).plan
    # 99 is not a first-layer colour and 1 appears twice: both spots go back to
    # auto; the good pin at the 4th position stays
    assert plan.first_layer_order == [1, None, None, 7]
    assert plan.layer_orders[min(plan.layer_orders)][3] == 7


def test_pinned_first_layer_order_is_written_into_the_project():
    project = geometry_3mf()
    stage1 = plan_from_project(project, slots=4, first_layer_order=[1, 9, 2])
    plan = stage1.plan
    xml = threemf._read_zip(stage1.reordered_project)["Metadata/model_settings.config"].decode()
    ids = [int(x) for x in re.search(
        r'key="first_layer_print_sequence" value="([^"]*)"', xml).group(1).split()]
    new_id = {
        old: plan.filament_colour.index(plan.original_filaments[old - 1]) + 1
        for old in range(1, 10)
    }
    assert ids[:3] == [new_id[c] for c in (1, 9, 2)]
    assert sorted(ids) == sorted(new_id.values())


def test_reordered_project_carries_the_chosen_layer_order():
    """The project is handed back with the plan's colour order set on the plate."""
    project = geometry_3mf()
    stage1 = plan_from_project(project, slots=4)
    plan = stage1.plan
    assert stage1.reorder_report.sequence_written
    assert stage1.reorder_report.sequence_layers == max(plan.layer_orders)

    xml = threemf._read_zip(stage1.reordered_project)["Metadata/model_settings.config"].decode()
    first = re.search(r'key="first_layer_print_sequence" value="([^"]*)"', xml)
    assert first
    ids = [int(x) for x in first.group(1).split()]
    # the first entry is the colour printed first, in the project's new numbering
    first_hex = plan.original_filaments[plan.first_colour - 1]
    assert ids[0] == plan.filament_colour.index(first_hex) + 1
    # and the later layers are covered by an open-ended block
    other = re.search(r'key="other_layers_print_sequence" value="([^"]*)"', xml)
    nums = re.search(r'key="other_layers_print_sequence_nums" value="(\d+)"', xml)
    assert other and nums and int(nums.group(1)) >= 1
    assert "2147483646" in other.group(1)


def test_unsliced_without_a_sequence_stays_deferred():
    """No sequence data at all: there is nothing to plan from yet."""
    project = build_3mf(sliced=False, with_sequences=False)
    stage1 = plan_from_project(project, slots=4)
    plan = stage1.plan
    assert plan.deferred
    assert not plan.provisional
    assert plan.pauses == []
    assert plan.filament_colour == FILAMENTS  # order left as it was
    assert any("no print sequence" in n for n in stage1.notes)


def test_unsliced_project_plans_provisionally_then_finalises_on_slice():
    project = build_3mf(sliced=False)
    plan = plan_from_project(project, slots=4).plan
    assert plan.provisional
    # the first-layer order only covers the start of the print, so it must not
    # be used as a hard check against the slice
    assert plan.expected_demands == []
    # the user slices with the planned order and uploads the slice
    old_to_new = {FILAMENTS.index(h): i for i, h in enumerate(plan.filament_colour)}
    resliced = keychain_gcode(remap=old_to_new, filaments=plan.filament_colour)
    result = insert_into_slice(resliced.encode("utf-8"), plan)
    assert result.report.ok, result.report.failures()
    assert result.edited.inserted_blocks == 6
    assert result.plan is not None
    assert not result.plan.deferred and not result.plan.provisional


def test_unsliced_with_sequence_json_is_provisional():
    project = build_3mf(sliced=False, sequence_json=True)
    stage1 = plan_from_project(project, slots=4)
    plan = stage1.plan
    assert plan.provisional
    # the sequence is Bambu's own first-layer order; the plan is exact on pauses
    assert plan.expected_demands
    assert len(plan.pauses) >= 1


def test_plate_selection():
    project = build_3mf()
    info = threemf.read_project(project, plate=1)
    assert info.plate == 1
    assert info.has_slice
    assert [p.id for p in info.plates] == [1]
    assert info.plate_by_id(1).name == "Keychain"
    # asking for a plate that does not exist falls back to the one that is sliced
    info2 = threemf.read_project(project, plate=2)
    assert info2.has_slice


def test_full_two_stage_flow():
    project = build_3mf()
    stage1 = plan_from_project(project, slots=4)
    plan = stage1.plan
    assert len(plan.pauses) == 6
    assert plan.expected_filament_order == plan.filament_colour
    assert plan.printer_model == "Bambu Lab A1"

    # the user re-slices: same geometry, tools renumbered to the new order
    old_to_new = {}
    for new, hexv in enumerate(plan.filament_colour):
        old_to_new[FILAMENTS.index(hexv)] = new
    resliced = parse_slice_simulation(old_to_new, plan.filament_colour)

    stage3 = insert_into_slice(resliced.encode("utf-8"), plan)
    assert stage3.report.ok, stage3.report.failures()
    assert stage3.edited.text.count("MANUAL SPOOL SWAP") == 12  # 6 start + 6 end


def test_existing_pauses_are_kept():
    """Pauses added in the slicer survive: only its own blocks are inserted."""
    project = build_3mf()
    stage1 = plan_from_project(project, slots=4)
    plan = stage1.plan
    old_to_new = {FILAMENTS.index(h): i for i, h in enumerate(plan.filament_colour)}
    resliced = parse_slice_simulation(old_to_new, plan.filament_colour)

    # the user adds a pause mid-print and one right before the first swap anchor
    lines = resliced.splitlines(keepends=True)
    user = "M400 U1 ; user paused to insert a magnet\n"
    lines.insert(len(lines) // 3, user)
    first_t = next(i for i, ln in enumerate(lines) if ln.strip().startswith("M620"))
    lines.insert(first_t, user)
    paused = "".join(lines)

    before_u1 = sum(1 for ln in paused.splitlines() if ln.strip().startswith("M400 U1"))
    result = insert_into_slice(paused.encode("utf-8"), plan)

    assert result.report.ok, result.report.failures()
    assert result.edited.text.count("user paused to insert a magnet") == 2
    after_u1 = sum(
        1 for ln in result.edited.text.splitlines() if ln.strip().startswith("M400 U1")
    )
    assert after_u1 == before_u1 + len(plan.pauses)


def test_stage3_stops_on_original_slice():
    project = build_3mf()
    plan = plan_from_project(project, slots=4).plan
    # uploading the ORIGINAL slice (old filament order) must be refused
    with pytest.raises(StopAndExplain):
        insert_into_slice(keychain_gcode().encode("utf-8"), plan)


def test_stage3_stops_on_wrong_printer():
    project = build_3mf()
    plan = plan_from_project(project, slots=4).plan
    old_to_new = {FILAMENTS.index(hexv): new for new, hexv in enumerate(plan.filament_colour)}
    other = keychain_gcode(remap=old_to_new, filaments=plan.filament_colour).replace(
        "Bambu Lab A1", "Bambu Lab X1C"
    )
    with pytest.raises(StopAndExplain):
        insert_into_slice(other.encode("utf-8"), plan)


def parse_slice_simulation(old_to_new: dict[int, int], filaments: list[str]) -> str:
    return keychain_gcode(remap=old_to_new, filaments=filaments)


def test_repack_slice_puts_the_edited_gcode_back():
    """A sliced upload goes out as a sliced project: project + G-code + checksum."""
    original = build_3mf()
    edited = keychain_gcode().replace("G90", "; a pause went here\nG90", 1)

    out = threemf.repack_slice(original, edited, "Metadata/plate_1.gcode")
    before = threemf._read_zip(original)
    after = threemf._read_zip(out)

    assert set(after) == set(before)                       # no entries lost
    assert after["Metadata/plate_1.gcode"].decode() == edited
    assert after["Metadata/plate_1.gcode.md5"].decode() == (
        hashlib.md5(edited.encode("utf-8")).hexdigest().upper()
    )
    for name in before:
        if name not in ("Metadata/plate_1.gcode", "Metadata/plate_1.gcode.md5"):
            assert after[name] == before[name]             # everything else verbatim

    with pytest.raises(StopAndExplain):
        threemf.repack_slice(original, edited, "Metadata/plate_9.gcode")


def test_plan_roundtrip():
    plan = plan_from_project(build_3mf(), slots=4).plan
    again = plan_from_dict(json.loads(json.dumps(plan_to_dict(plan))))
    assert again.pauses[0].swaps[0].describe() == plan.pauses[0].swaps[0].describe()
    assert again.initial == plan.initial
    assert again.expected_tools == plan.expected_tools


def test_single_stage_convert():
    result = plan_and_insert(keychain_gcode().encode("utf-8"), slots=4)
    assert result.report.ok
    assert result.edited.inserted_blocks == 6
