from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pausepalette import colours  # noqa: E402
from pausepalette.editor import insert_pauses  # noqa: E402
from pausepalette.gcode import parse_gcode  # noqa: E402
from pausepalette.planner import plan_for_gcode  # noqa: E402
from pausepalette.verifier import verify  # noqa: E402
from tests.fixtures import keychain_gcode  # noqa: E402


def _analysis():
    gf = parse_gcode(keychain_gcode())
    plan = plan_for_gcode(gf, slots=4)
    return gf, plan


def test_parser_reads_header_and_blocks():
    gf = parse_gcode(keychain_gcode())
    assert gf.filaments == [
        "#000000", "#FF0000", "#8B4513", "#FFD700", "#DFFF00",
        "#404040", "#FFDAB9", "#C0C0C0", "#FFFFFF",
    ]
    assert gf.printer_model == "Bambu Lab A1"
    # 9 + 8 + 8 + 12*2 = 49 tool changes
    assert len(gf.tool_changes) == 49
    assert gf.tool_changes[0].tool == 0
    assert gf.tool_changes[0].layer == 1
    assert gf.tool_changes[3].tool == 1  # pause 1 anchors here
    assert all(b.unload_line is not None for b in gf.tool_changes)
    assert all(b.refeed_start is not None for b in gf.tool_changes)


def test_names_and_luminance():
    assert colours.name_for_hex("#000000") == "Black"
    assert colours.name_for_hex("#404040") == "Dark grey"
    assert colours.name_for_hex("#FFDAB9") == "Peach"
    assert colours.name_for_hex("#DFFF00") == "Neon yellow"
    assert colours.name_for_hex("#C0C0C0") == "Silver"
    assert colours.luminance("#000000") < colours.luminance("#404040")
    assert colours.luminance("#404040") < colours.luminance("#8B4513")
    assert colours.luminance("#8B4513") < colours.luminance("#FF0000")


def test_plan_matches_prompt_six_pauses():
    gf, plan = _analysis()
    # initial AMS load, darkest -> lightest (rule 6)
    assert plan.initial == [0, 5, 2, 1]  # Pos1 Black, Pos2 Dark grey, Pos3 Brown, Pos4 Red
    assert len(plan.pauses) == 6
    # 15 swaps — one better than the hand-written prompt plan's 16
    assert plan.swaps_total == 15
    # anchors: the DP optimum sits one block later in places than the
    # hand-written prompt plan, buying one fewer swap for the same pause count
    anchors = [(p.anchor_ordinal, p.tool, p.layer) for p in plan.pauses]
    assert anchors == [
        (4, 7, 1),
        (5, 6, 1),
        (11, 6, 2),
        (14, 2, 2),
        (19, 1, 3),
        (22, 3, 3),
    ]


def test_plan_final_state_stocks_body_colours():
    gf, plan = _analysis()
    final = plan.timeline[-1].positions
    assert 0 in final  # Black resident
    assert 8 in final  # White resident


def test_editor_inserts_and_verifier_passes():
    gf, plan = _analysis()
    result = insert_pauses(gf, plan)
    assert result.inserted_blocks == 6
    assert result.edited_blocks == 6
    # 6 new pauses + the pre-existing end-of-print M400 U1
    u1 = [ln for ln in result.text.splitlines() if ln.strip().startswith("M400 U1")]
    assert len(u1) == 7
    assert result.text.count("filament already unloaded at pause") == 12  # 2 per block

    report = verify(gf, result, plan)
    assert report.ok, report.failures()

    # tool sequence in the output is identical to the original
    edited = parse_gcode(result.text)
    assert [(b.tool, b.layer) for b in edited.tool_changes] == [
        (b.tool, b.layer) for b in gf.tool_changes
    ]


def test_no_pauses_when_colours_fit():
    gf = parse_gcode(keychain_gcode())
    plan = plan_for_gcode(gf, slots=9)
    assert plan.pauses == []
    result = insert_pauses(gf, plan)
    assert result.text == gf.text()


def test_original_is_never_modified():
    text = keychain_gcode()
    gf = parse_gcode(text)
    plan = plan_for_gcode(gf, slots=4)
    result = insert_pauses(gf, plan)
    assert gf.text() == text
    assert result.text != text
