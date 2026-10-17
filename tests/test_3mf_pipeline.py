from __future__ import annotations

import json
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
from tests.make_3mf import build_3mf, project_settings  # noqa: E402


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

    # stale slice stripped, everything else preserved
    assert "Metadata/plate_1.gcode" not in threemf._read_zip(new_data)
    assert report.slices_stripped


def test_reorder_detects_bad_permutation():
    with pytest.raises(StopAndExplain):
        threemf.reorder_project(build_3mf(), [0, 1, 2])


def test_stage1_missing_slice_stops():
    with pytest.raises(StopAndExplain):
        plan_from_project(build_3mf(sliced=False), slots=4)


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
