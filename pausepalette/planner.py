"""The colour planner.

Finds the pause-minimal, swap-minimal, position-stable, dark-to-light slot
schedule for a tool-change timeline. Exact search — no heuristics that can miss
the optimum, no AI.

Objective (lexicographic, smallest first):
    1. fewest pauses
    2. fewest spool swaps
    3. fewest re-loads (sticky-home tiebreak)
    4. dark-to-light bias (darker colours swapped in earlier)

The model is the classical offline slot-scheduling problem: a state is the set
of colours loaded in the AMS, a pause is a transition between sets, and every
demand between two pauses must be covered by the set loaded at that pause.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from . import colours as C
from .model import GcodeFile, Pause, Plan, PlannedStep, Swap

Cost = tuple[int, int, int, float]
INITIAL_COST: Cost = (0, 0, 0, 0.0)


def _set_swaps(a: frozenset[int], b: frozenset[int]) -> int:
    """Number of physical positions that change between two loaded sets."""
    return max(len(a - b), len(b - a))


@dataclass
class _Node:
    cost: Cost
    prev_i: int
    prev_key: frozenset[int] | None
    action: str               # "start" | "go" | "pause"
    new_set: frozenset[int] | None = None
    pause_at: int = -1        # demand index the pause sits before


def _collapse(block_tools: list[int]) -> tuple[list[int], list[int]]:
    """Return (demands, run_start_block_index) collapsing consecutive runs."""
    demands: list[int] = []
    starts: list[int] = []
    prev = None
    for idx, tool in enumerate(block_tools):
        if tool != prev:
            demands.append(tool)
            starts.append(idx)
            prev = tool
    return demands, starts


def plan_slots(
    block_tools: list[int],
    block_layers: list[int],
    lums: dict[int, float],
    names: dict[int, str],
    slots: int,
    initial: set[int] | None = None,
) -> Plan:
    if slots < 1:
        raise ValueError("slots must be >= 1")

    demands, run_starts = _collapse(block_tools)
    plan = Plan(
        slots=slots,
        demands=demands,
        blocks=list(block_tools),
        block_layers=list(block_layers),
        initial=[],
        pauses=[],
        swaps_total=0,
        colour_moves=0,
    )
    if not demands:
        return plan

    distinct: list[int] = []
    for t in demands:
        if t not in distinct:
            distinct.append(t)

    if initial is not None:
        initial_set = frozenset(initial)
        if demands[0] not in initial_set:
            # the very first colour must be resident at print start, otherwise no
            # pause could ever load it
            keep = sorted(initial_set)[: max(0, slots - 1)]
            initial_set = frozenset(keep + [demands[0]])
            plan.warnings.append(
                f"initial load did not contain the first printed colour "
                f"(T{demands[0]}); it was added"
            )
    else:
        initial_set = frozenset(distinct[:slots])

    if len(distinct) <= slots:
        plan.initial = _assign_positions(initial_set, slots, lums, {})
        plan.timeline = [PlannedStep(label="start", positions=list(plan.initial))]
        return plan

    # ---- dynamic program over (demand index, loaded set) ----------------
    states: list[dict[frozenset[int], _Node]] = [dict() for _ in range(len(demands) + 1)]
    states[0][initial_set] = _Node(INITIAL_COST, -1, None, "start")

    def relax(i: int, key: frozenset[int], node: _Node) -> None:
        bucket = states[i]
        cur = bucket.get(key)
        if cur is None or node.cost < cur.cost:
            bucket[key] = node

    for i in range(len(demands)):
        need = demands[i]
        demanded_before = set(demands[:i])
        # Only a bounded lookahead of colours is worth preloading: a colour further
        # out than the next few distinct demands cannot lengthen the coverage of
        # this pause, so enumerating the whole suffix is wasted work.
        window = max(slots - 1, 2 * slots)
        extras_pool: list[int] = []
        for t in demands[i + 1 :]:
            if t != need and t not in extras_pool:
                extras_pool.append(t)
                if len(extras_pool) >= window:
                    break
        for loaded, node in list(states[i].items()):
            if need in loaded:
                relax(
                    i + 1,
                    loaded,
                    _Node(node.cost, i, loaded, "go"),
                )
            if i == 0:
                continue
            for k in range(0, min(slots - 1, len(extras_pool)) + 1):
                for extra in itertools.combinations(extras_pool, k):
                    new_set = frozenset((need, *extra))
                    if new_set == loaded:
                        continue
                    swaps = _set_swaps(loaded, new_set)
                    added = new_set - loaded
                    reloads = len(added & demanded_before)
                    dark = node.cost[3] + node.cost[0] * sum(
                        lums.get(t, 0.5) for t in added
                    )
                    cost: Cost = (
                        node.cost[0] + 1,
                        node.cost[1] + swaps,
                        node.cost[2] + reloads,
                        dark,
                    )
                    relax(
                        i + 1,
                        new_set,
                        _Node(cost, i, loaded, "pause", new_set, pause_at=i),
                    )

    terminal = states[len(demands)]
    if not terminal:
        raise RuntimeError("planner found no valid schedule")
    best_key = min(terminal, key=lambda k: terminal[k].cost)

    # reconstruct: list of (pause_at_demand_index, new_set)
    pause_actions: list[tuple[int, frozenset[int]]] = []
    key = best_key
    i = len(demands)
    while True:
        node = states[i][key]
        if node.action == "pause" and node.new_set is not None:
            pause_actions.append((node.pause_at, node.new_set))
        if node.prev_key is None:
            break
        key = node.prev_key
        i = node.prev_i
    pause_actions.reverse()

    # ---- positional reconstruction --------------------------------------
    placed = _assign_positions(initial_set, slots, lums, {})
    plan.initial = list(placed)
    plan.timeline = [PlannedStep(label="start", positions=list(placed))]
    current = set(initial_set)
    pauses: list[Pause] = []
    prev_anchor_demand = -1
    occupied: dict[int, set[int]] = {t: set() for t in distinct}
    for p_idx, (demand_i, new_set) in enumerate(pause_actions, start=1):
        previous = {pos: tool for pos, tool in enumerate(placed, start=1) if tool in new_set}
        new_placed = _assign_positions(new_set, slots, lums, previous)
        swaps = _position_swaps(placed, new_placed, names)
        for pos, tool in enumerate(new_placed, start=1):
            if tool is not None:
                occupied.setdefault(tool, set()).add(pos)
        evicted = current - new_set
        anchor_demand = _anchor_demand(demands, demand_i, evicted, prev_anchor_demand)
        prev_anchor_demand = anchor_demand
        anchor_ordinal = run_starts[anchor_demand]
        pauses.append(
            Pause(
                index=p_idx,
                demand_index=demand_i,
                anchor_ordinal=anchor_ordinal,
                layer=block_layers[anchor_ordinal] if block_layers else 1,
                tool=block_tools[anchor_ordinal],
                swaps=swaps,
            )
        )
        placed = new_placed
        current = set(new_set)
        plan.timeline.append(PlannedStep(label=f"pause {p_idx}", positions=list(placed)))

    plan.pauses = pauses
    plan.swaps_total = sum(len(p.swaps) for p in pauses)
    plan.colour_moves = sum(max(0, len(pos) - 1) for pos in occupied.values())
    return plan


def _anchor_demand(
    demands: list[int],
    pause_demand: int,
    evicted: set[int],
    prev_anchor: int,
) -> int:
    """The tool-change block the pause is inserted before, as a demand index.

    Earliest block after every evicted colour has finished, never before the
    previous pause's anchor, and never after the demand that needs the new
    colours.
    """
    last_evicted = -1
    for d in range(pause_demand):
        if demands[d] in evicted:
            last_evicted = d
    anchor = pause_demand if last_evicted < 0 else last_evicted + 1
    anchor = max(anchor, prev_anchor + 1)
    anchor = min(anchor, pause_demand)
    return anchor


def _assign_positions(
    loaded: frozenset[int] | set[int],
    slots: int,
    lums: dict[int, float],
    previous: dict[int, int],
) -> list[int | None]:
    """Place a loaded set into positions, keeping existing homes where possible.

    Free positions are filled darkest-first (rule 6).
    """
    positions: list[int | None] = [None] * slots
    remaining = set(loaded)
    for pos, tool in previous.items():
        if 1 <= pos <= slots and tool in remaining:
            positions[pos - 1] = tool
            remaining.discard(tool)
    free = [p for p in range(1, slots + 1) if positions[p - 1] is None]
    for tool, pos in zip(sorted(remaining, key=lambda t: lums.get(t, 0.5)), free):
        positions[pos - 1] = tool
    return positions


def _position_swaps(
    old: list[int | None],
    new: list[int | None],
    names: dict[int, str],
) -> list[Swap]:
    out: list[Swap] = []
    for pos in range(1, len(new) + 1):
        o = old[pos - 1] if pos - 1 < len(old) else None
        n = new[pos - 1]
        if o == n:
            continue
        out.append(
            Swap(
                position=pos,
                out_tool=o,
                in_tool=n,
                out_name=names.get(o, "empty") if o is not None else "empty",
                in_name=names.get(n, "empty") if n is not None else "empty",
            )
        )
    return out


# ---- convenience ---------------------------------------------------------


def _lums_and_names(gf: GcodeFile) -> tuple[dict[int, float], dict[int, str]]:
    names = {i: n for i, n in enumerate(gf.colour_names())}
    lums: dict[int, float] = {}
    for i, hexv in enumerate(gf.filaments):
        try:
            lums[i] = C.luminance(hexv)
        except ValueError:
            lums[i] = 0.5
    return lums, names


def plan_for_gcode(gf: GcodeFile, slots: int, initial: set[int] | None = None) -> Plan:
    lums, names = _lums_and_names(gf)
    return plan_slots(
        block_tools=[b.tool for b in gf.tool_changes],
        block_layers=[b.layer for b in gf.tool_changes],
        lums=lums,
        names=names,
        slots=slots,
        initial=initial,
    )
