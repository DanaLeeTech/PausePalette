"""Per-printer pause blocks: every Bambu model unloads its own way."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pausepalette.dialects import bambu  # noqa: E402
from pausepalette.dialects.bambu_models import BAMBU_UNLOAD  # noqa: E402
from pausepalette.editor import insert_pauses  # noqa: E402
from pausepalette.gcode import parse_gcode  # noqa: E402
from pausepalette.model import Pause, Swap  # noqa: E402
from pausepalette.planner import plan_for_gcode  # noqa: E402
from pausepalette.verifier import verify  # noqa: E402
from tests.fixtures import keychain_gcode  # noqa: E402


def _swap() -> Pause:
    return Pause(
        index=1,
        demand_index=0,
        anchor_ordinal=0,
        layer=1,
        tool=0,
        swaps=[Swap(position=1, out_tool=0, in_tool=4, out_name="Black", in_name="Silver")],
    )


def _block(model: str | None) -> str:
    return "\n".join(
        bambu.build_pause_block(_swap(), 1, "\n", "0.4", "1200", 220, printer_model=model)
    )


def test_every_bambu_model_has_an_unload_sequence():
    # the models Bambu Studio ships profiles for
    for model in [
        "A1", "A1 mini", "A2L", "H2C", "H2D", "H2D Pro", "H2S",
        "P1P", "P1S", "P2S", "X1", "X1 Carbon", "X1E", "X2D",
    ]:
        assert BAMBU_UNLOAD[model], model
        # each sequence opens an AMS channel, changes pseudo-tool, closes it
        assert BAMBU_UNLOAD[model][0].startswith("M620 S"), model
        assert any(l.startswith("T") for l in BAMBU_UNLOAD[model]), model
        assert BAMBU_UNLOAD[model][-1].startswith("M621 S"), model


def test_the_a1_block_cuts_at_the_a1_cutter():
    block = _block("Bambu Lab A1")
    assert "G1 X267 F15000" in block      # the A1's cutter
    assert "T255" in block
    assert "M400 U1" in block


def test_the_x1_block_parks_where_the_x1_parks():
    block = _block("Bambu Lab X1 Carbon")
    assert "G1 X20 Y50 F12000" in block
    assert "G1 Y-3" in block
    assert "X267" not in block            # no cutter on an X1
    assert "T255" in block


def test_the_newer_models_unload_with_the_65535_pseudo_tool():
    for model, tool in [("Bambu Lab H2D", "T65535"), ("Bambu Lab P2S", "T65535"),
                        ("Bambu Lab X2D", "T65279")]:
        block = _block(model)
        assert tool in block, model
        assert "T255" not in block, model
        assert "M400 U1" in block, model


def test_every_block_waits_for_the_user_and_is_marked():
    for model in list(BAMBU_UNLOAD) + [None]:
        block = _block(model)
        assert "M400 U1 ; PAUSE: swap spools now, then resume" in block
        assert "; ===== MANUAL SPOOL SWAP 1 of 1 =====" in block
        assert "; ===== END MANUAL SPOOL SWAP 1 =====" in block
        assert "; swap: Pos1: Black -> Silver" in block


def test_a_file_for_another_bambu_model_plans_and_verifies_end_to_end():
    for model in ["Bambu Lab X1 Carbon", "Bambu Lab H2D", "Bambu Lab A1 mini"]:
        text = keychain_gcode().replace(
            "; printer_model = Bambu Lab A1", f"; printer_model = {model}"
        )
        gf = parse_gcode(text)
        plan = plan_for_gcode(gf, slots=4)
        result = insert_pauses(gf, plan)

        expected = bambu.unload_marker(model)
        assert result.text.count(f"\n{expected}\n") >= len(plan.pauses)
        report = verify(gf, result, plan)
        assert report.ok, (model, report.failures())
        # the printer's own moves, not the A1's
        if model == "Bambu Lab X1 Carbon":
            assert "G1 X20 Y50 F12000" in result.text
            assert "G1 X267" not in result.text


def test_an_unknown_printer_falls_back_and_warns():
    text = keychain_gcode().replace(
        "; printer_model = Bambu Lab A1", "; printer_model = Bambu Lab Z99"
    )
    gf = parse_gcode(text)
    plan = plan_for_gcode(gf, slots=4)
    result = insert_pauses(gf, plan)

    assert bambu.unload_for("Bambu Lab Z99") is None
    assert "G1 X267 F15000" in result.text          # the A1 sequence, as a fallback
    assert any("not one of the printers" in w for w in plan.warnings)
    assert verify(gf, result, plan).ok


def test_a_file_with_no_printer_model_still_plans():
    text = keychain_gcode().replace("; printer_model = Bambu Lab A1\n", "")
    gf = parse_gcode(text)
    plan = plan_for_gcode(gf, slots=4)
    result = insert_pauses(gf, plan)
    assert "M400 U1" in result.text
    assert verify(gf, result, plan).ok
