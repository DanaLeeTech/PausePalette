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
    initial: list[int | None] | None = None,
) -> list[int]:
    """New filament order (list of old tool indices) from a tool sequence.

    The colour loaded in AMS slot 1 must be T0, slot 2 must be T1, and so on, so
    the plan's slot order wins when it has one: colours that are never swapped
    come first, then the rest. Without a plan (a reorder with no schedule yet)
    the initial `slots` colours go first, darkest first, and the rest follow
    first-need order with a dark-to-light tiebreak.
    """
    distinct: list[int] = []
    for t in sequence:
        if t not in distinct:
            distinct.append(t)

    if initial:
        initial_tools = [t for t in initial if t is not None][:slots]
        # anything the plan did not load initially still has to be ordered
        for t in distinct:
            if len(initial_tools) >= slots:
                break
            if t not in initial_tools:
                initial_tools.append(t)
    else:
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


def renumber_plan(plan: Plan, order: list[int]) -> Plan:
    """Return a copy of the plan with every tool index mapped into a new order.

    ``order`` is the new filament list as old tool indices (``order[new] = old``).
    """
    old_to_new = {old: new for new, old in enumerate(order)}

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
                remaining=p.remaining,
            )
        )
    return new


def _sequence_from_orders(layer_orders: dict[int, list[int]]) -> tuple[list[int], list[int]]:
    """The print's tool changes from a colour order for each layer.

    Returns ``(tools, layers)``, tools 0-based and layers 1-based. A colour that
    is already loaded carries over, so a colour used at the end of one layer and
    the start of the next costs no change.
    """
    tools: list[int] = []
    layers: list[int] = []
    active: int | None = None
    for layer in sorted(layer_orders):
        for tool in layer_orders[layer]:
            if tool == active:
                continue
            tools.append(tool - 1)
            layers.append(layer)
            active = tool
    return tools, layers


def _sequence_from_layers(
    plate: threemf.PlateInfo,
    used: dict[int, set[int]],
    last_layer: int,
) -> tuple[list[int], list[int]]:
    """As above, but using the order the project itself stored for each layer."""
    orders: dict[int, list[int]] = {}
    for layer in range(1, last_layer + 1):
        present = used.get(layer)
        if not present:
            continue
        order = [t for t in plate.order_for_layer(layer) if t in present]
        orders[layer] = order + sorted(t for t in present if t not in order)
    return _sequence_from_orders(orders)


#: how many colour swaps the order search may try before it settles
ORDER_SEARCH_BUDGET = 300
#: how far apart two colours may be swapped while searching
ORDER_SEARCH_SPAN = 6


def choose_layer_orders(
    used: dict[int, set[int]],
    lums: dict[int, float],
    names: dict[int, str],
    slots: int,
    first_colour: int | None = None,
    budget: int = ORDER_SEARCH_BUDGET,
    max_span: int = ORDER_SEARCH_SPAN,
) -> dict[int, list[int]]:
    """The colour print order for each layer: fewest pauses, then fewest swaps.

    The first layer starts with ``first_colour`` (the darkest colour by default)
    and each later layer with the colour the one before it ended on, so nothing
    needs swapping at a layer boundary; the rest of each layer follows darkest to
    lightest. A bounded search then swaps colours within a layer while that
    improves the plan, so the result is never worse than those rules alone.
    """
    layers = sorted(used)
    if not layers:
        return {}

    # ``used`` holds 1-based filament ids; ``lums`` is indexed by tool
    def luminance(colour: int) -> float:
        return lums.get(colour - 1, 0.5)

    darkest = min(used[layers[0]], key=lambda t: (luminance(t), t))
    first = first_colour if first_colour in used[layers[0]] else darkest

    def darkest_first(colours):
        return sorted(colours, key=lambda t: (luminance(t), t))

    orders: dict[int, list[int]] = {}
    previous: int | None = None
    for layer in layers:
        present = used[layer]
        if layer == layers[0] and first in present:
            start = [first]
        elif previous in present:
            start = [previous]
        else:
            start = []
        orders[layer] = start + darkest_first(t for t in present if t not in start)
        previous = orders[layer][-1]

    def key(candidate: dict[int, list[int]]):
        return tuple((layer, tuple(candidate[layer])) for layer in layers)

    def cost(candidate: dict[int, list[int]], thorough: bool = False) -> tuple[int, int]:
        # the search scores many candidates, so it uses the cheaper planner;
        # the winner is checked properly before it is returned
        tools, block_layers = _sequence_from_orders(candidate)
        plan = plan_slots(tools, block_layers, lums, names, slots=slots, thorough=thorough)
        return (len(plan.pauses), plan.swaps_total)

    # only a layer with more colours than slots has an order worth choosing:
    # where they all fit, every colour is resident before it is used
    if all(len(used[layer]) <= slots for layer in layers):
        return orders

    rules = {k: list(v) for k, v in orders.items()}
    scores = {key(orders): cost(orders)}
    best = scores[key(orders)]
    movable = [layer for layer in layers if len(orders[layer]) > 1]
    tried = 0
    improved = True
    while improved and tried < budget:
        improved = False
        for layer in movable:
            size = len(orders[layer])
            for span in range(1, max_span + 1):
                for i in range(1, size - span):  # position 0 is the first colour
                    if tried >= budget:
                        break
                    candidate = {k: list(v) for k, v in orders.items()}
                    candidate[layer][i], candidate[layer][i + span] = (
                        candidate[layer][i + span],
                        candidate[layer][i],
                    )
                    tried += 1
                    score = scores.get(key(candidate))
                    if score is None:
                        score = cost(candidate)
                        scores[key(candidate)] = score
                    if score < best:
                        orders, best, improved = candidate, score, True

    # the search scored candidates with the cheaper planner: keep whichever of
    # the plain rules and the searched order the exact planner prefers (ties go
    # to the rules, which start each layer on the colour the last one ended on)
    if cost(orders, thorough=True) >= cost(rules, thorough=True):
        return rules
    return orders


def plan_from_project(
    data: bytes, slots: int = 4, plate: int = 1, first_colour: int | None = None
) -> Stage1Result:
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
        if len(gf.filaments) != len(info.filaments):
            raise StopAndExplain(
                f"this project is inconsistent: it lists {len(info.filaments)} "
                f"filament(s) but the slice inside it names {len(gf.filaments)}. "
                f"PausePalette cannot reorder a project whose filament list and "
                f"slice disagree — re-export the project from Bambu Studio / "
                f"OrcaSlicer and try again."
            )
        plan = plan_for_gcode(gf, slots)
        sequence = [b.tool for b in gf.tool_changes]
        order = choose_filament_order(
            sequence, _filename_lums(gf), slots, len(gf.filaments), initial=plan.initial
        )
        new_plan = renumber_plan(plan, order)
        _attach_plan_details(new_plan, gf, order=order)
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
            block_layers=[1] * len(tools),
            lums=lums,
            names=names,
            slots=slots,
        )
        plan.provisional = True
        order = choose_filament_order(
            tools, lums, slots, len(info.filaments), initial=plan.initial
        )
        new_plan = renumber_plan(plan, order)
        new_plan.expected_demands = list(new_plan.demands)
        notes.append(
            "this project is not sliced, but Bambu Studio stored its filament "
            "sequence — the plan below is provisional: the exact pause positions "
            "are resolved when you upload the slice."
        )
    else:
        # ---- unsliced: work the whole print out from the model -----------
        # The model says which filament each part prints with and how tall it
        # is, so the layers each colour appears on can be recovered — that is
        # the whole print, not just its start. PausePalette then chooses the
        # colour order for each layer (fewest pauses, then fewest swaps) and
        # writes it into the project, so the slicer prints that order.
        lums = _lums_for(info.filaments)
        names = {i: n for i, n in enumerate(_assign_colour_letters(info.filaments))}
        used: dict[int, set[int]] = {}
        layer_orders: dict[int, list[int]] = {}
        tools: list[int] = []
        block_layers: list[int] = []
        from_model = False
        if chosen is not None and info.layer_height:
            geometry = threemf.filament_layers(data, chosen, info.layer_height)
            if geometry is not None:
                by_colour, _model_layers = geometry
                for colour, where in by_colour.items():
                    if 1 <= colour <= len(info.filaments):
                        for layer in where:
                            used.setdefault(layer, set()).add(colour)
                layer_orders = choose_layer_orders(
                    used, lums, names, slots, first_colour=first_colour
                )
                tools, block_layers = _sequence_from_orders(layer_orders)
                from_model = bool(tools)
        if not from_model and chosen is not None:
            # no geometry to read: fall back to the layers Bambu stored an order
            # for (the tail's order alone cannot say which colours it prints)
            layer_orders = {}
            tools = []
            block_layers = []
            usable = len(info.filaments)
            first = [x - 1 for x in chosen.first_layer_sequence if 0 <= x - 1 < usable]
            tools.extend(first)
            block_layers.extend([1] * len(first))
            for layer in sorted(chosen.layer_orders):
                more = [x - 1 for x in chosen.layer_orders[layer] if 0 <= x - 1 < usable]
                tools.extend(more)
                block_layers.extend([layer] * len(more))
        if tools:
            plan = plan_slots(
                block_tools=tools,
                block_layers=block_layers,
                lums=lums,
                names=names,
                slots=slots,
            )
            plan.provisional = True
            order = choose_filament_order(
                tools, lums, slots, len(info.filaments), initial=plan.initial
            )
            new_plan = renumber_plan(plan, order)
            # The sequence is read from the model rather than a slice, so it is
            # not used as a hard check; stage 3 re-plans from the slice.
            new_plan.expected_demands = []
            known = sorted(set(block_layers))
            span = (
                f"layers {known[0]}–{known[-1]}" if len(known) > 1 else f"layer {known[0]}"
            )
            if from_model:
                new_plan.layer_orders = {k: list(v) for k, v in layer_orders.items()}
                first_layer = min(layer_orders)
                new_plan.first_colour = layer_orders[first_layer][0]
                new_plan.first_colour_choices = sorted(used[first_layer])
                notes.append(
                    f"this project is not sliced; the plan below is the whole print, "
                    f"worked out from the model's own geometry — which filament each "
                    f"layer prints with. The colour order for each layer was chosen to "
                    f"need the fewest pauses and is written into the project, so the "
                    f"slicer prints it. It is provisional: the exact pause count and "
                    f"positions are confirmed when you upload the slice."
                )
            else:
                notes.append(
                    f"this project is not sliced; the plan below is provisional, built "
                    f"from the colour order Bambu Studio stored for {span}. Layers after "
                    f"that, and the exact pause count and positions, are resolved when "
                    f"you upload the slice."
                )
        else:
            order = list(range(len(info.filaments)))
            plan = Plan(slots=slots, demands=[], blocks=[], block_layers=[], initial=[],
                        pauses=[], swaps_total=0, colour_moves=0)
            plan.deferred = True
            new_plan = renumber_plan(plan, order)
            new_plan.expected_demands = []
            notes.append(
                "this project is not sliced and carries no print sequence, so the "
                "filament order is left as it is and the pause plan is computed "
                "when you upload the slice."
            )

    if not new_plan.pauses and not new_plan.deferred:
        if new_plan.provisional:
            notes.append(
                f"the colours in the stored layers fit in {slots} slots — "
                f"the full schedule is resolved from your slice"
            )
        else:
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
    _name_swaps(new_plan)

    reordered, report = threemf.reorder_project(
        data, order, layer_orders=new_plan.layer_orders or None, plate=new_plan.plate
    )
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


def _name_swaps(plan: Plan) -> None:
    for p in plan.pauses:
        for s in p.swaps:
            s.out_name = plan.tool_name(s.out_tool)
            s.in_name = plan.tool_name(s.in_tool)


def _attach_plan_details(plan: Plan, gf: GcodeFile, order: list[int] | None = None) -> Plan:
    """Everything a plan needs from the slice it will be carried out on.

    The same for every printer — planning and reordering never look at the
    machine, only the G-code edit does (see :func:`insert_pauses`). ``order`` is
    the new filament order (old tool index per new T) when the plan reorders the
    list, ``None`` when the file's own numbering stands because it is already
    sliced.
    """
    if order is None:
        order = list(range(len(gf.filaments)))
    new_of = {old: new for new, old in enumerate(order)}

    plan.filament_colour = [gf.filaments[o] for o in order if o < len(gf.filaments)]
    plan.filament_name = [C.name_for_hex(c) for c in plan.filament_colour]
    plan.printer_model = plan.printer_model or gf.printer_model
    plan.expected_tools = [new_of.get(b.tool, b.tool) for b in gf.tool_changes]
    plan.expected_layers = [b.layer for b in gf.tool_changes]
    plan.expected_demands = list(plan.demands)
    _name_swaps(plan)
    return plan


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


def insert_into_slice(data: bytes, plan: Plan, newline: str | None = None) -> Stage3Result:
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
        resolved.plate = plan.plate
        _attach_plan_details(resolved, gf)
        resolved.printer_model = plan.printer_model or gf.printer_model
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
    newline: str | None = None,
    plate: int | None = None,
) -> Stage3Result:
    text, name = _slice_text(gcode_bytes, plate=plate)
    gf = parse_gcode(text)
    plan = plan_for_gcode(gf, slots)
    _attach_plan_details(plan, gf)
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
        "layer_orders": {str(layer): ids for layer, ids in plan.layer_orders.items()},
        "first_colour": plan.first_colour,
        "first_colour_choices": plan.first_colour_choices,
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
                "remaining": p.remaining,
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
        layer_orders={
            int(layer): list(ids)
            for layer, ids in (d.get("layer_orders") or {}).items()
        },
        first_colour=d.get("first_colour"),
        first_colour_choices=list(d.get("first_colour_choices", [])),
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
                remaining=p.get("remaining"),
            )
        )
    return plan
