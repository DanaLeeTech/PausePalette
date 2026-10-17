"""The two-stage pipeline and its single-stage shortcut.

Stage 1  plan_from_project(3mf)      -> plan + reordered project + instructions
Stage 2  (the user reorders, slices and exports — outside the app)
Stage 3  insert_into_slice(gcode)    -> edited G-code + verification report

Single-stage: plan_and_insert(gcode) does both at once for a file that is
already sliced and already in its final filament order.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import colours as C
from . import threemf
from .editor import insert_pauses
from .errors import StopAndExplain
from .gcode import parse_gcode
from .model import GcodeFile, InsertionResult, Plan, Pause, Swap, VerificationReport, PlannedStep
from .planner import plan_for_gcode
from .verifier import verify


# ---------------------------------------------------------------------------
# Stage 1
# ---------------------------------------------------------------------------


@dataclass
class Stage1Result:
    plan: Plan
    project_entries: list[str]
    reorder_report: threemf.ReorderReport
    reordered_project: bytes
    original_filaments: list[str]
    original_names: list[str]
    printer_model: str | None
    notes: list[str] = field(default_factory=list)


def choose_filament_order(gf: GcodeFile, plan: Plan) -> list[int]:
    """New filament order (list of old tool indices).

    Rule 6: the initial four become T0..T3 in position order (darkest first);
    the rest follow first-need order with a dark-to-light tiebreak.
    """
    distinct: list[int] = []
    for t in [b.tool for b in gf.tool_changes]:
        if t not in distinct:
            distinct.append(t)

    initial_tools = [t for t in plan.initial if t is not None]
    order: list[int] = list(initial_tools)

    lums = {}
    for i, hexv in enumerate(gf.filaments):
        try:
            lums[i] = C.luminance(hexv)
        except ValueError:
            lums[i] = 0.5

    first_need: dict[int, int] = {}
    for idx, tool in enumerate([b.tool for b in gf.tool_changes]):
        first_need.setdefault(tool, idx)

    rest = [t for t in distinct if t not in order]
    rest.sort(key=lambda t: (first_need.get(t, 10**9), lums.get(t, 0.5), t))
    order.extend(rest)
    # any tool that appears in the file but not in the demand timeline
    for t in sorted(set(b.tool for b in gf.tool_changes)):
        if t not in order:
            order.append(t)
    return order


def renumber_plan(plan: Plan, old_to_new: dict[int, int]) -> Plan:
    """Return a copy of the plan with every tool index mapped to new numbering."""
    def m(t: int | None) -> int | None:
        return None if t is None else old_to_new[t]

    new = Plan(
        slots=plan.slots,
        demands=[m(t) for t in plan.demands],
        blocks=[m(t) for t in plan.blocks],
        block_layers=list(plan.block_layers),
        initial=[m(t) for t in plan.initial],
        pauses=[],
        swaps_total=plan.swaps_total,
        colour_moves=plan.colour_moves,
        timeline=[
            PlannedStep(label=s.label, positions=[m(t) for t in s.positions])
            for s in plan.timeline
        ],
        warnings=list(plan.warnings),
        printer_model=plan.printer_model,
    )
    for p in plan.pauses:
        new.pauses.append(
            Pause(
                index=p.index,
                demand_index=p.demand_index,
                anchor_ordinal=p.anchor_ordinal,
                layer=p.layer,
                tool=m(p.tool),
                swaps=[
                    Swap(
                        position=s.position,
                        out_tool=m(s.out_tool),
                        in_tool=m(s.in_tool),
                        out_name=s.out_name,
                        in_name=s.in_name,
                    )
                    for s in p.swaps
                ],
                z=p.z,
                feed=p.feed,
                temp=p.temp,
            )
        )
    return new


def plan_from_project(data: bytes, slots: int = 4) -> Stage1Result:
    info = threemf.read_project(data)
    if not info.has_slice or not info.slice_gcode:
        raise StopAndExplain(
            "this project has no sliced plate stored in it. Slice it once in "
            "Bambu Studio (the slice is saved with the project), then upload the "
            "project again — the plan needs the exact tool-change sequence, which "
            "only the slice contains."
        )

    gf = parse_gcode(info.slice_gcode)
    if not gf.tool_changes:
        raise StopAndExplain("the project's slice has no AMS tool changes — nothing to plan")

    plan = plan_for_gcode(gf, slots)
    notes: list[str] = []
    if not plan.pauses:
        notes.append(
            f"all {len(set(plan.demands))} colours fit in {slots} slots — no pauses are needed"
        )

    order = choose_filament_order(gf, plan)
    old_to_new = {old: i for i, old in enumerate(order)}

    new_plan = renumber_plan(plan, old_to_new)
    new_plan.expected_tools = [old_to_new.get(b.tool, b.tool) for b in gf.tool_changes]
    new_plan.expected_layers = [b.layer for b in gf.tool_changes]
    new_plan.expected_filament_order = [gf.filaments[o] for o in order]
    new_plan.filament_colour = [gf.filaments[o] for o in order]
    new_plan.filament_name = [C.name_for_hex(gf.filaments[o]) for o in order]
    new_plan.printer_model = info.printer_model
    # Re-point swap names at the new numbering.
    for p in new_plan.pauses:
        for s in p.swaps:
            s.out_name = new_plan.tool_name(s.out_tool)
            s.in_name = new_plan.tool_name(s.in_tool)

    reordered, report = threemf.reorder_project(data, order)
    for w in report.warnings:
        notes.append(w)
    if info.reference_keys:
        notes.append(
            "filament references remapped: "
            + ", ".join(f"{k}={v}" for k, v in sorted(info.reference_keys.items()))
        )

    return Stage1Result(
        plan=new_plan,
        project_entries=info.entries,
        reorder_report=report,
        reordered_project=reordered,
        original_filaments=info.filaments,
        original_names=[C.name_for_hex(c) for c in info.filaments],
        printer_model=info.printer_model,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Stage 3
# ---------------------------------------------------------------------------


@dataclass
class Stage3Result:
    edited: InsertionResult
    report: VerificationReport
    slice_name: str | None = None
    notes: list[str] = field(default_factory=list)
    plan: Plan | None = None


def _slice_text(data: bytes) -> tuple[str, str | None]:
    if threemf.looks_like_zip(data):
        got = threemf.extract_slice(data)
        if got is None:
            raise StopAndExplain(
                "this .3mf has no sliced plate inside it — export the plate sliced "
                "file from Bambu Studio and upload that"
            )
        name, text = got
        return text, name
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        raise StopAndExplain(f"could not read the uploaded file as G-code ({exc})") from exc


def check_slice_against_plan(gf: GcodeFile, plan: Plan) -> None:
    """Hard checks — stop-and-explain on any mismatch."""
    if not plan.expected_tools:
        raise StopAndExplain(
            "this job has no stage-1 plan (upload the project first, or use the "
            "single-stage route for an already-sliced file)"
        )

    if plan.printer_model:
        model = gf.printer_model or ""
        if model and model != plan.printer_model:
            raise StopAndExplain(
                f"the uploaded slice is for '{model}' but the plan was made for "
                f"'{plan.printer_model}' — slice it for the right printer"
            )

    if plan.filament_colour:
        got = [c.upper() for c in gf.filaments]
        want = [c.upper() for c in plan.filament_colour]
        if got != want:
            raise StopAndExplain(
                "the uploaded slice's filament list does not match the plan's "
                "ordering. Reorder the filaments in Bambu Studio exactly as the "
                "plan shows, re-slice and export again.\n"
                f"  plan: {';'.join(want)}\n"
                f"  file: {';'.join(got)}"
            )

    got_tools = [b.tool for b in gf.tool_changes]
    got_layers = [b.layer for b in gf.tool_changes]
    if got_tools != plan.expected_tools or got_layers != plan.expected_layers:
        diffs = []
        for i, (a, b) in enumerate(zip(plan.expected_tools, got_tools)):
            if a != b:
                diffs.append(f"block {i}: expected T{a}, found T{b}")
                if len(diffs) >= 5:
                    break
        raise StopAndExplain(
            "the uploaded slice's tool-change sequence differs from the plan "
            f"({len(plan.expected_tools)} blocks expected, {len(got_tools)} found). "
            "The model or paint must have changed since the project was uploaded. "
            + ("; ".join(diffs) if diffs else "")
        )


def insert_into_slice(data: bytes, plan: Plan, newline: str = "\n") -> Stage3Result:
    text, name = _slice_text(data)
    gf = parse_gcode(text)
    check_slice_against_plan(gf, plan)

    # fill the pause z / feed / temp from the actual blocks before editing
    by_ordinal = {b.ordinal: b for b in gf.tool_changes}
    for pause in plan.pauses:
        block = by_ordinal.get(pause.anchor_ordinal)
        if block is not None:
            pause.temp = pause.temp or None
    edited = insert_pauses(gf, plan, newline=newline)
    report = verify(gf, edited, plan)
    return Stage3Result(edited=edited, report=report, slice_name=name, notes=list(plan.warnings))


# ---------------------------------------------------------------------------
# Single-stage shortcut
# ---------------------------------------------------------------------------


def plan_and_insert(gcode_bytes: bytes, slots: int = 4, newline: str = "\n") -> Stage3Result:
    text, name = _slice_text(gcode_bytes)
    gf = parse_gcode(text)
    plan = plan_for_gcode(gf, slots)
    plan.filament_colour = list(gf.filaments)
    plan.filament_name = list(gf.colour_names())
    plan.printer_model = gf.printer_model
    for p in plan.pauses:
        for s in p.swaps:
            s.out_name = plan.tool_name(s.out_tool)
            s.in_name = plan.tool_name(s.in_tool)
    edited = insert_pauses(gf, plan, newline=newline)
    report = verify(gf, edited, plan)
    return Stage3Result(
        edited=edited, report=report, slice_name=name, notes=list(plan.warnings), plan=plan
    )


# ---------------------------------------------------------------------------
# serialisation for the job store
# ---------------------------------------------------------------------------


def plan_to_dict(plan: Plan) -> dict:
    return {
        "slots": plan.slots,
        "demands": plan.demands,
        "blocks": plan.blocks,
        "block_layers": plan.block_layers,
        "initial": plan.initial,
        "swaps_total": plan.swaps_total,
        "colour_moves": plan.colour_moves,
        "printer_model": plan.printer_model,
        "expected_tools": plan.expected_tools,
        "expected_layers": plan.expected_layers,
        "expected_filament_order": plan.expected_filament_order,
        "filament_colour": plan.filament_colour,
        "filament_name": plan.filament_name,
        "warnings": plan.warnings,
        "timeline": [{"label": s.label, "positions": s.positions} for s in plan.timeline],
        "pauses": [
            {
                "index": p.index,
                "demand_index": p.demand_index,
                "anchor_ordinal": p.anchor_ordinal,
                "layer": p.layer,
                "tool": p.tool,
                "swaps": [
                    {
                        "position": s.position,
                        "out_tool": s.out_tool,
                        "in_tool": s.in_tool,
                        "out_name": s.out_name,
                        "in_name": s.in_name,
                    }
                    for s in p.swaps
                ],
                "z": p.z,
                "feed": p.feed,
                "temp": p.temp,
            }
            for p in plan.pauses
        ],
    }


def plan_from_dict(d: dict) -> Plan:
    plan = Plan(
        slots=d["slots"],
        demands=list(d.get("demands", [])),
        blocks=list(d.get("blocks", [])),
        block_layers=list(d.get("block_layers", [])),
        initial=list(d.get("initial", [])),
        pauses=[],
        swaps_total=d.get("swaps_total", 0),
        colour_moves=d.get("colour_moves", 0),
        timeline=[
            PlannedStep(label=s["label"], positions=list(s["positions"]))
            for s in d.get("timeline", [])
        ],
        warnings=list(d.get("warnings", [])),
        printer_model=d.get("printer_model"),
        expected_tools=list(d.get("expected_tools", [])),
        expected_layers=list(d.get("expected_layers", [])),
        expected_filament_order=list(d.get("expected_filament_order", [])),
        filament_colour=list(d.get("filament_colour", [])),
        filament_name=list(d.get("filament_name", [])),
    )
    for p in d.get("pauses", []):
        plan.pauses.append(
            Pause(
                index=p["index"],
                demand_index=p["demand_index"],
                anchor_ordinal=p["anchor_ordinal"],
                layer=p["layer"],
                tool=p["tool"],
                swaps=[
                    Swap(
                        position=s["position"],
                        out_tool=s["out_tool"],
                        in_tool=s["in_tool"],
                        out_name=s["out_name"],
                        in_name=s["in_name"],
                    )
                    for s in p["swaps"]
                ],
                z=p.get("z", "0.4"),
                feed=p.get("feed", "1200"),
                temp=p.get("temp"),
            )
        )
    return plan
