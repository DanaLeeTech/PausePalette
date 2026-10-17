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
from .planner import plan_for_gcode, plan_slots
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
    plate: int | None = None
    plates: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _assign_colour_letters(filaments: list[str]) -> list[str]:
    return [C.name_for_hex(c) for c in filaments]


def choose_filament_order(
    sequence: list[int],
    lums: dict[int, float],
    slots: int,
    n_filaments: int,
) -> list[int]:
    """New filament order (list of old tool indices) from a tool sequence.

    Rule 6: the initial `slots` colours become T0.., ordered darkest first; the
    rest follow first-need order with a dark-to-light tiebreak.
    """
    distinct: list[int] = []
    for t in sequence:
        if t not in distinct:
            distinct.append(t)

    initial_tools = sorted(distinct[:slots], key=lambda t: (lums.get(t, 0.5), t))
    order: list[int] = list(initial_tools)

    first_need: dict[int, int] = {}
    for idx, tool in enumerate(sequence):
        first_need.setdefault(tool, idx)

    rest = [t for t in distinct if t not in order]
    rest.sort(key=lambda t: (first_need.get(t, 10**9), lums.get(t, 0.5), t))
    order.extend(rest)
    for t in range(n_filaments):
        if t not in order:
            order.append(t)
    return order


def _lums_for(filaments: list[str]) -> dict[int, float]:
    lums: dict[int, float] = {}
    for i, hexv in enumerate(filaments):
        try:
            lums[i] = C.luminance(hexv)
        except ValueError:
            lums[i] = 0.5
    return lums


def _filename_lums(gf: GcodeFile) -> dict[int, float]:
    return _lums_for(gf.filaments)


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
        plate=plan.plate,
        provisional=plan.provisional,
        deferred=plan.deferred,
        expected_demands=[m(t) for t in plan.expected_demands],
        original_filaments=list(plan.original_filaments),
        original_names=list(plan.original_names),
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


def plan_from_project(data: bytes, slots: int = 4, plate: int = 1) -> Stage1Result:
    info = threemf.read_project(data, plate=plate)
    notes: list[str] = []
    plates_meta = [
        {
            "id": p.id,
            "name": p.name,
            "has_slice": p.has_gcode,
            "sequence": len(p.sequence),
        }
        for p in info.plates
    ]
    chosen = info.plate_by_id(plate)
    if chosen is not None and chosen.has_gcode:
        # ---- sliced project: exact plan with anchors --------------------
        gf = parse_gcode(info.slice_gcode or "")
        if not gf.tool_changes:
            raise StopAndExplain(
                "the project's slice has no AMS tool changes — nothing to plan"
            )
        plan = plan_for_gcode(gf, slots)
        sequence = [b.tool for b in gf.tool_changes]
        order = choose_filament_order(sequence, _filename_lums(gf), slots, len(gf.filaments))
        old_to_new = {old: i for i, old in enumerate(order)}
        new_plan = renumber_plan(plan, old_to_new)
        new_plan.expected_tools = [old_to_new.get(b.tool, b.tool) for b in gf.tool_changes]
        new_plan.expected_layers = [b.layer for b in gf.tool_changes]
        new_plan.expected_demands = [old_to_new[t] for t in plan.demands]
    elif chosen is not None and chosen.sequence:
        # ---- unsliced but Bambu stored the tool sequence ----------------
        tools = [x - 1 for x in chosen.sequence]
        if any(t < 0 or t >= len(info.filaments) for t in tools):
            raise StopAndExplain(
                "the project's stored filament sequence refers to filaments that "
                "are not in the project's filament list"
            )
        lums = _lums_for(info.filaments)
        names = {i: n for i, n in enumerate(_assign_colour_letters(info.filaments))}
        plan = plan_slots(
            block_tools=tools,
            block_layers=[0] * len(tools),
            lums=lums,
            names=names,
            slots=slots,
        )
        plan.provisional = True
        order = choose_filament_order(tools, lums, slots, len(info.filaments))
        old_to_new = {old: i for i, old in enumerate(order)}
        new_plan = renumber_plan(plan, old_to_new)
        new_plan.expected_demands = [old_to_new[t] for t in plan.demands]
        notes.append(
            "this project is not sliced, but Bambu Studio stored its filament "
            "sequence — the plan below is provisional: the exact pause positions "
            "are resolved when you upload the slice."
        )
    else:
        # ---- unsliced and no sequence: reorder now, plan at stage 3 ------
        sequence = []
        if chosen is not None and chosen.first_layer_sequence:
            sequence = [x - 1 for x in chosen.first_layer_sequence
                        if 0 <= x - 1 < len(info.filaments)]
        if sequence:
            order = choose_filament_order(
                sequence, _lums_for(info.filaments), slots, len(info.filaments)
            )
            notes.append(
                "this project is not sliced; the filament order was set from the "
                "stored first-layer print sequence. The pause plan is computed "
                "when you upload the slice."
            )
        else:
            order = list(range(len(info.filaments)))
            notes.append(
                "this project is not sliced and carries no print sequence, so the "
                "filament order is left as it is and the pause plan is computed "
                "when you upload the slice."
            )
        lums = _lums_for(info.filaments)
        names = {i: n for i, n in enumerate(_assign_colour_letters(info.filaments))}
        plan = Plan(slots=slots, demands=[], blocks=[], block_layers=[], initial=[],
                    pauses=[], swaps_total=0, colour_moves=0)
        plan.deferred = True
        old_to_new = {old: i for i, old in enumerate(order)}
        new_plan = renumber_plan(plan, old_to_new)
        new_plan.expected_demands = []

    if not new_plan.pauses and not new_plan.deferred:
        notes.append(
            f"all {len(set(new_plan.demands))} colours fit in {slots} slots — "
            f"no pauses are needed"
        )

    new_plan.expected_filament_order = [info.filaments[o] for o in order]
    new_plan.filament_colour = [info.filaments[o] for o in order]
    new_plan.filament_name = [C.name_for_hex(info.filaments[o]) for o in order]
    new_plan.printer_model = info.printer_model
    new_plan.plate = info.plate if chosen is not None else None
    new_plan.original_filaments = list(info.filaments)
    new_plan.original_names = _assign_colour_letters(info.filaments)
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
        original_names=_assign_colour_letters(info.filaments),
        printer_model=info.printer_model,
        plate=new_plan.plate,
        plates=plates_meta,
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


def _name_swaps(plan: Plan) -> None:
    for p in plan.pauses:
        for s in p.swaps:
            s.out_name = plan.tool_name(s.out_tool)
            s.in_name = plan.tool_name(s.in_tool)


def check_slice_against_plan(gf: GcodeFile, plan: Plan) -> None:
    """Hard checks — stop-and-explain on any mismatch."""
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

    if plan.expected_tools:
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
    elif plan.expected_demands:
        got = [b.tool for b in gf.tool_changes]
        collapsed: list[int] = []
        prev = None
        for t in got:
            if t != prev:
                collapsed.append(t)
                prev = t
        if collapsed != plan.expected_demands:
            raise StopAndExplain(
                "the uploaded slice's colour sequence differs from the sequence the "
                "provisional plan was made from. Re-slice the reordered project "
                "without changing the paint, and upload that."
            )


def _slice_text(data: bytes, plate: int | None = None) -> tuple[str, str | None]:
    if threemf.looks_like_zip(data):
        got = threemf.extract_slice(data, plate=plate)
        if got is None:
            raise StopAndExplain(
                "this .3mf has no sliced plate inside it — export the plate sliced "
                "file from Bambu Studio and upload that"
            )
        name, text = got
        if plate is not None and not name.endswith(f"plate_{plate}.gcode"):
            # the plan was made for a different plate
            try:
                names = [
                    n
                    for n in threemf._read_zip(data)
                    if threemf.SLICE_FILES.match(n) and n.endswith(".gcode")
                ]
            except Exception:  # noqa: BLE001
                names = []
            if len(names) > 1:
                raise StopAndExplain(
                    f"this .3mf contains several sliced plates and none is plate "
                    f"{plate} (the plate this plan was made for). Upload the slice "
                    f"for plate {plate}, or start a new job for another plate."
                )
        return text, name
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        raise StopAndExplain(f"could not read the uploaded file as G-code ({exc})") from exc


def insert_into_slice(data: bytes, plan: Plan, newline: str = "\n") -> Stage3Result:
    text, name = _slice_text(data, plate=plan.plate)
    gf = parse_gcode(text)
    check_slice_against_plan(gf, plan)

    notes = list(plan.warnings)
    if plan.deferred or plan.provisional:
        # resolve the anchors on the actual slice now that we have it
        initial = set(t for t in plan.initial if t is not None) if plan.provisional else None
        if initial is not None and not initial:
            initial = None
        resolved = plan_for_gcode(gf, plan.slots, initial=initial)
        resolved.printer_model = plan.printer_model or gf.printer_model
        resolved.plate = plan.plate
        resolved.filament_colour = list(gf.filaments)
        resolved.filament_name = list(gf.colour_names())
        resolved.expected_tools = [b.tool for b in gf.tool_changes]
        resolved.expected_layers = [b.layer for b in gf.tool_changes]
        _name_swaps(resolved)
        notes.append(
            "the pause schedule was finalised from this slice"
            + (" (same as the provisional plan)" if plan.provisional else "")
        )
        plan = resolved

    edited = insert_pauses(gf, plan, newline=newline)
    report = verify(gf, edited, plan)
    return Stage3Result(edited=edited, report=report, slice_name=name, notes=notes, plan=plan)


# ---------------------------------------------------------------------------
# Single-stage shortcut
# ---------------------------------------------------------------------------


def plan_and_insert(
    gcode_bytes: bytes,
    slots: int = 4,
    newline: str = "\n",
    plate: int | None = None,
) -> Stage3Result:
    text, name = _slice_text(gcode_bytes, plate=plate)
    gf = parse_gcode(text)
    plan = plan_for_gcode(gf, slots)
    plan.filament_colour = list(gf.filaments)
    plan.filament_name = list(gf.colour_names())
    plan.printer_model = gf.printer_model
    plan.expected_tools = [b.tool for b in gf.tool_changes]
    plan.expected_layers = [b.layer for b in gf.tool_changes]
    plan.expected_demands = list(plan.demands)
    _name_swaps(plan)
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
        "plate": plan.plate,
        "provisional": plan.provisional,
        "deferred": plan.deferred,
        "expected_tools": plan.expected_tools,
        "expected_layers": plan.expected_layers,
        "expected_demands": plan.expected_demands,
        "expected_filament_order": plan.expected_filament_order,
        "filament_colour": plan.filament_colour,
        "filament_name": plan.filament_name,
        "original_filaments": plan.original_filaments,
        "original_names": plan.original_names,
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
        plate=d.get("plate"),
        provisional=bool(d.get("provisional", False)),
        deferred=bool(d.get("deferred", False)),
        expected_tools=list(d.get("expected_tools", [])),
        expected_layers=list(d.get("expected_layers", [])),
        expected_demands=list(d.get("expected_demands", [])),
        expected_filament_order=list(d.get("expected_filament_order", [])),
        filament_colour=list(d.get("filament_colour", [])),
        filament_name=list(d.get("filament_name", [])),
        original_filaments=list(d.get("original_filaments", [])),
        original_names=list(d.get("original_names", [])),
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
