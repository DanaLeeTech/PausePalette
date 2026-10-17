from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pausepalette.editor import insert_pauses  # noqa: E402
from pausepalette.gcode import parse_gcode  # noqa: E402
from pausepalette.planner import plan_for_gcode  # noqa: E402
from pausepalette.verifier import verify  # noqa: E402
from tests.fixtures import keychain_gcode, tool_block  # noqa: E402


def _gcode_with(tool_sequence: list[int]) -> str:
    lines = ["; filament_colour = " + ";".join(f"#{i:02x}{(i*7) % 256:02x}{(i*13) % 256:02x}" for i in range(16))]
    for tool in tool_sequence:
        lines.append("; layer num/total_layer_count: 1/1")
        lines.extend(tool_block(tool))
    return "\n".join(lines) + "\n"


def test_one_slot_pauses_at_every_change():
    gf = parse_gcode(keychain_gcode())
    plan = plan_for_gcode(gf, slots=1)
    # with a single slot every colour change needs a pause
    assert len(plan.pauses) == len(plan.demands) - 1
    result = insert_pauses(gf, plan)
    assert verify(gf, result, plan).ok


def test_more_slots_than_colours_needs_no_pauses():
    gf = parse_gcode(keychain_gcode())
    plan = plan_for_gcode(gf, slots=16)
    assert plan.pauses == []
    assert insert_pauses(gf, plan).text == gf.text()


def test_planner_scales():
    rng = random.Random(7)
    seq = [rng.randrange(16) for _ in range(160)]
    # avoid consecutive duplicates (they collapse anyway)
    text = _gcode_with(seq)
    gf = parse_gcode(text)
    start = time.time()
    plan = plan_for_gcode(gf, slots=4)
    elapsed = time.time() - start
    assert elapsed < 20, f"planner too slow: {elapsed:.1f}s"
    result = insert_pauses(gf, plan)
    assert verify(gf, result, plan).ok


def test_two_ams_units_eight_slots():
    # nine colours in eight slots: by farthest-future reasoning exactly three
    # pauses are needed (one at White's first use, then two reloads of whichever
    # colour was evicted while the rotating colour is out)
    gf = parse_gcode(keychain_gcode())
    plan = plan_for_gcode(gf, slots=8)
    assert len(plan.pauses) == 3
    result = insert_pauses(gf, plan)
    assert verify(gf, result, plan).ok


def test_colours_fit_single_ams_four_slots_still_valid():
    rng = random.Random(3)
    seq = []
    for _ in range(10):
        seq.extend(rng.sample(range(4), 4))
    gf = parse_gcode(_gcode_with(seq))
    plan = plan_for_gcode(gf, slots=4)
    # only four distinct colours -> nothing ever needs swapping
    assert plan.pauses == []
