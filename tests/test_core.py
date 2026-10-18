from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pausepalette import colours  # noqa: E402
from pausepalette.editor import insert_pauses  # noqa: E402
from pausepalette.gcode import parse_gcode  # noqa: E402
from pausepalette.model import Pause  # noqa: E402
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
    # anchors: each pause sits at the earliest block that stabs every swap window
    # it serves, and every colour keeps the position it started in
    anchors = [(p.anchor_ordinal, p.tool, p.layer) for p in plan.pauses]
    assert anchors == [
        (4, 7, 1),
        (6, 3, 1),
        (11, 6, 2),
        (14, 2, 2),
        (20, 7, 3),
        (22, 3, 3),
    ]


def test_pause_names_the_colour_before_and_after():
    """The page's 'after the swap' / 'last printed' labels come from the plan."""
    gf, plan = _analysis()
    for pause in plan.pauses:
        # the block the pause precedes is printed once the swaps are done...
        assert plan.tool_after(pause) == plan.blocks[pause.anchor_ordinal]
        # ...and the block before it is the colour the nozzle was holding
        assert plan.tool_before(pause) == plan.blocks[pause.anchor_ordinal - 1]
        assert plan.tool_before(pause) != plan.tool_after(pause)

    # a pause in front of the very first block has nothing printed before it
    opening = Pause(index=1, demand_index=0, anchor_ordinal=0, layer=1, tool=0, swaps=[])
    assert plan.tool_before(opening) is None


def test_every_colour_keeps_one_position():
    """The hard rule: a spool never moves between AMS positions, ever."""
    gf, plan = _analysis()
    homes: dict[int, set[int]] = {}
    for step in plan.timeline:
        for pos, tool in enumerate(step.positions, start=1):
            if tool is not None:
                homes.setdefault(tool, set()).add(pos)
    for tool, positions in homes.items():
        assert len(positions) == 1, f"T{tool} used positions {sorted(positions)}"
    # ...and the metric agrees
    assert plan.colour_moves == 0
    # every colour that is swapped in is swapped into its home
    for pause in plan.pauses:
        for swap in pause.swaps:
            assert swap.in_tool is None or homes[swap.in_tool] == {swap.position}


def test_reloaded_colour_returns_to_its_own_position():
    """A colour evicted at one pause comes back to the same slot later."""
    gf, plan = _analysis()
    home: dict[int, int] = {}
    for step in plan.timeline:
        for pos, tool in enumerate(step.positions, start=1):
            if tool is not None:
                home.setdefault(tool, pos)
    seen_loaded: dict[int, int] = {}
    for step in plan.timeline:
        for pos, tool in enumerate(step.positions, start=1):
            if tool is None:
                continue
            assert home[tool] == pos
            seen_loaded[tool] = pos
    # the plan actually exercises eviction and reload, not just one load each
    reloaded = [t for t in seen_loaded if any(
        st.positions[home[t] - 1] != t for st in plan.timeline
    )]
    assert reloaded, "expected at least one colour to be swapped out and back"


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


def test_parse_time_dhms():
    from pausepalette.gcode import parse_time_dhms

    assert parse_time_dhms("1d 8h 29m 45s") == 116985.0
    assert parse_time_dhms("2h 41m") == 9660.0
    assert parse_time_dhms("38m 12s") == 2292.0
    assert parse_time_dhms("21s") == 21.0
    assert parse_time_dhms("45") == 45.0  # bare seconds
    assert parse_time_dhms("not a time") is None


def test_header_time_prefers_total_over_model():
    gf = parse_gcode(keychain_gcode())
    assert gf.estimated_time == 2292.0  # 'total estimated time', not 'model printing time'
    assert gf.header["total_estimated_time"] == "38m 12s"


def test_pauses_get_remaining_time():
    gf, plan = _analysis()
    remaining = [p.remaining for p in plan.pauses]
    assert all(r is not None for r in remaining)
    # time left shrinks as the print goes on, and never exceeds the whole print
    assert remaining == sorted(remaining, reverse=True)
    assert all(0.0 < r <= gf.estimated_time for r in remaining)
    assert remaining[0] > remaining[-1]
    # the pause sits after the layer-1 flush work, so it is not the whole print
    assert remaining[0] < gf.estimated_time
    assert [p.remaining_text for p in plan.pauses] == [
        "35m", "33m", "29m", "27m", "22m", "21m",
    ]


def test_no_time_header_leaves_remaining_unset():
    text = "\n".join(
        line
        for line in keychain_gcode().splitlines()
        if "estimated time" not in line and "model printing time" not in line
    )
    gf = parse_gcode(text)
    plan = plan_for_gcode(gf, slots=4)
    assert gf.estimated_time is None
    assert all(p.remaining is None for p in plan.pauses)
    assert all(p.remaining_text == "" for p in plan.pauses)


def test_orca_time_header_at_end_of_file():
    """OrcaSlicer writes its estimate at the end, outside the header block."""
    text = "\n".join(
        line
        for line in keychain_gcode().splitlines()
        if "estimated time" not in line and "model printing time" not in line
    )
    text += (
        "\n; estimated printing time (normal mode) = 2h 41m 33s"
        "\n; estimated printing time (silent mode) = 2h 45m 12s"
        "\n; estimated first layer printing time (normal mode) = 5m 3s\n"
    )
    gf = parse_gcode(text)
    # the normal-mode line wins; the first-layer line is not the whole print
    assert gf.estimated_time == 9693.0

