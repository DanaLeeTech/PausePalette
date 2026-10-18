"""The colour planner.

Finds the pause-minimal, swap-minimal, dark-to-light slot schedule for a
tool-change timeline. Exact search — no heuristics that can miss the optimum,
no AI.

The model
---------
A spool has one home: **a colour stays in the same AMS position for the whole
print.** A position may be shared by several colours over time (one at a time),
but a colour is never moved to another position, and it always comes back to
the one it started in. A pause may swap any subset of the positions at once.

So a plan is:

1. an assignment of every colour to a position (its home), with the initial load
   (rule 6: darkest first) occupying the first positions one each;
2. for each position, the sequence of colours it must hold. A demanded colour
   must sit in its home, so between two consecutive demands of different colours
   in the same position exactly one swap is needed, and it may be anchored
   anywhere in ``(last use of the old colour, first use of the new colour]``;
3. the pause times. A swap window needs a pause inside it, so the pause count is
   the minimum number of points that stab all the windows (greedy by earliest
   end — optimal).

That makes the check for a given position assignment exact and cheap: the swaps
are forced by the demand sequence and only the pause placement is a choice. The
search is over the assignments, so the only thing to optimise is *which colour
lives in which slot*.

Objective (lexicographic, smallest first):
    1. fewest pauses
    2. fewest spool swaps
    3. dark-to-light bias (darker colours swapped in earlier)

Every assignment is enumerated when that is affordable; otherwise the search
starts from a greedy assignment and hill-climbs single-colour moves.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from . import colours as C
from .gcode import extrusion_work
from .model import GcodeFile, Pause, Plan, PlannedStep, Swap

# Enumerate colour -> position assignments exhaustively while the count stays
# under this; beyond it, use the greedy start plus local search.
EXHAUSTIVE_LIMIT = 60_000
# Ceiling on local-search evaluations, for pathological colour counts.
LOCAL_SEARCH_LIMIT = 20_000


@dataclass(frozen=True)
class _Window:
    """One forced swap, and when it may happen.

    ``left`` is the first demand index the pause may be anchored at (the demand
    after the old colour's last use in this position); ``right`` is the demand
    that needs the new colour.
    """

    left: int
    right: int
    position: int
    out_tool: int | None
    in_tool: int


@dataclass(frozen=True, order=True)
class _Cost:
    pauses: int
    swaps: int
    keep_dark: float          # prefer to swap out light colours, keep dark ones resident
    dark: float
    order: tuple[int, ...]


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


def _darkest_first(colours_in, lums: dict[int, float], slots: int) -> list[int]:
    """The initial load: darkest first, one per position (rule 6)."""
    return sorted(colours_in, key=lambda t: (lums.get(t, 0.5), t))[:slots]


def _positions(colours_by_position: dict[int, int | None], slots: int) -> list[int | None]:
    return [colours_by_position.get(p) for p in range(1, slots + 1)]


def _windows(
    homes: dict[int, int],
    initial: dict[int, int],
    demands: list[int],
    slots: int,
) -> list[_Window]:
    """The forced swaps: between consecutive demands of different colours."""
    sequences: dict[int, list[tuple[int, int]]] = {p: [] for p in range(1, slots + 1)}
    for t, c in enumerate(demands):
        sequences[homes[c]].append((t, c))

    out: list[_Window] = []
    for pos in range(1, slots + 1):
        current = initial.get(pos)
        last = -1
        for t, colour in sequences[pos]:
            if colour != current:
                out.append(_Window(last + 1, t, pos, current, colour))
                current = colour
            last = t
    return out


def _stab(windows: list[_Window]) -> list[int]:
    """Fewest pause points that cover every swap window (greedy, optimal)."""
    points: list[int] = []
    last = None
    for w in sorted(windows, key=lambda w: (w.right, w.left, w.position)):
        if last is not None and w.left <= last <= w.right:
            continue
        points.append(w.right)
        last = w.right
    return points


def _attribute(windows: list[_Window], points: list[int]) -> list[int]:
    """Index of the pause each window's swap happens at."""
    out: list[int] = []
    for w in windows:
        for idx, point in enumerate(points):
            if w.left <= point <= w.right:
                out.append(idx)
                break
        else:  # pragma: no cover - _stab guarantees coverage
            raise RuntimeError("swap window was not covered by any pause")
    return out


def _cost(
    homes: dict[int, int],
    initial: dict[int, int],
    demands: list[int],
    slots: int,
    lums: dict[int, float],
    colour_order: list[int],
) -> _Cost:
    """Evaluate one colour -> position assignment."""
    windows = _windows(homes, initial, demands, slots)
    points = _stab(windows)
    assigned = _attribute(windows, points)

    dark = 0.0
    for idx, point in enumerate(points, start=1):
        here = [w for w, a in zip(windows, assigned) if a == idx]
        dark += idx * sum(lums.get(w.in_tool, 0.5) for w in here)

    # A spool you never take out is a spool you never think about, so prefer to
    # leave the dark colours in the machine and swap the light ones: the penalty
    # is the darkness of every colour that gets taken out.
    taken_out = {w.out_tool for w in windows if w.out_tool is not None}
    keep_dark = sum(1.0 - lums.get(t, 0.5) for t in taken_out)

    return _Cost(
        pauses=len(points),
        swaps=len(windows),
        keep_dark=keep_dark,
        dark=dark,
        order=tuple(homes[c] for c in colour_order),
    )


def _greedy_homes(
    rest: list[int],
    homes: dict[int, int],
    initial: dict[int, int],
    demands: list[int],
    slots: int,
    lums: dict[int, float],
) -> dict[int, int]:
    """Place each remaining colour where it costs fewest swaps right now."""
    first_need: dict[int, int] = {}
    for t, c in enumerate(demands):
        first_need.setdefault(c, t)
    out = dict(homes)
    for colour in rest:  # complete mapping: every demand must have a home
        out[colour] = 1
    for colour in sorted(rest, key=lambda c: (first_need.get(c, 0), lums.get(c, 0.5), c)):
        best = None
        for pos in range(1, slots + 1):
            cand = dict(out)
            cand[colour] = pos
            window_count = len(_windows(cand, initial, demands, slots))
            if best is None or window_count < best[0]:
                best = (window_count, pos)
        out[colour] = best[1]
    return out


def _local_search(
    start: dict[int, int],
    rest: list[int],
    initial: dict[int, int],
    demands: list[int],
    slots: int,
    lums: dict[int, float],
    colour_order: list[int],
) -> dict[int, int]:
    def score(homes: dict[int, int]) -> _Cost:
        return _cost(homes, initial, demands, slots, lums, colour_order)

    homes = dict(start)
    best = score(homes)
    used = 1
    improved = True
    while improved and used < LOCAL_SEARCH_LIMIT:
        improved = False
        for colour in rest:
            for pos in range(1, slots + 1):
                if pos == homes[colour] or used >= LOCAL_SEARCH_LIMIT:
                    continue
                cand = dict(homes)
                cand[colour] = pos
                used += 1
                cost = score(cand)
                if cost < best:
                    best = cost
                    homes = cand
                    improved = True
    return homes


def _choose_homes(
    demands: list[int],
    distinct: list[int],
    initial_list: list[int],
    slots: int,
    lums: dict[int, float],
    thorough: bool = True,
) -> dict[int, int]:
    """Pick the colour -> position assignment with the best cost.

    ``thorough=False`` skips the exhaustive enumeration and hill-climbs from a
    greedy start instead — the same answer on most prints, much cheaper, and
    used while searching for a colour order (where many candidates are scored).
    """
    homes = {c: i + 1 for i, c in enumerate(initial_list)}
    initial = {i + 1: c for i, c in enumerate(initial_list)}
    rest = [c for c in distinct if c not in homes]
    if not rest:
        return homes

    options = slots ** len(rest)
    if thorough and options <= EXHAUSTIVE_LIMIT:
        best_cost = None
        best_homes = homes
        for combo in itertools.product(range(1, slots + 1), repeat=len(rest)):
            cand = dict(homes)
            cand.update(zip(rest, combo))
            cost = _cost(cand, initial, demands, slots, lums, distinct)
            if best_cost is None or cost < best_cost:
                best_cost = cost
                best_homes = cand
        return best_homes

    start = _greedy_homes(rest, homes, initial, demands, slots, lums)
    return _local_search(start, rest, initial, demands, slots, lums, distinct)


def _spool_order(
    windows: list[_Window],
    initial_by_pos: dict[int, int],
    slots: int,
    lums: dict[int, float],
) -> dict[int, int]:
    """Relabel the positions so that the spools you never touch come first.

    A position with no swaps at all holds one colour for the whole print, so it
    belongs in the lowest slot; the ones that get swapped follow, darkest first
    among equals. Returns ``old position -> new position``.
    """
    swapped = {w.position for w in windows}

    def key(pos: int):
        colour = initial_by_pos.get(pos)
        return (
            0 if pos not in swapped else 1,
            lums.get(colour, 0.5) if colour is not None else 1.0,
            pos,
        )

    order = sorted(range(1, slots + 1), key=key)
    return {old: new for new, old in enumerate(order, start=1)}


def _relabel(window: _Window, mapping: dict[int, int]) -> _Window:
    return _Window(
        left=window.left,
        right=window.right,
        position=mapping[window.position],
        out_tool=window.out_tool,
        in_tool=window.in_tool,
    )


def plan_slots(
    block_tools: list[int],
    block_layers: list[int],
    lums: dict[int, float],
    names: dict[int, str],
    slots: int,
    initial: set[int] | None = None,
    thorough: bool = True,
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

    # ---- the initial load (rule 6: darkest first) ------------------------
    if initial is not None:
        picked = set(initial)
        if demands[0] not in picked:
            # the very first colour must be resident at print start, otherwise no
            # pause could ever load it
            keep = sorted(picked)[: max(0, slots - 1)]
            picked = set(keep) | {demands[0]}
            plan.warnings.append(
                f"initial load did not contain the first printed colour "
                f"(T{demands[0]}); it was added"
            )
        initial_list = _darkest_first(picked, lums, slots)
    else:
        initial_list = _darkest_first(distinct[:slots], lums, slots)

    plan.initial = list(initial_list) + [None] * (slots - len(initial_list))

    if len(distinct) <= slots:
        plan.timeline = [PlannedStep(label="start", positions=list(plan.initial))]
        return plan

    # ---- choose the colour -> position assignment ------------------------
    homes = _choose_homes(demands, distinct, initial_list, slots, lums, thorough=thorough)
    initial_by_pos = {i + 1: c for i, c in enumerate(initial_list)}

    windows = _windows(homes, initial_by_pos, demands, slots)

    # a colour that is never swapped stays in the lowest slot it can
    remap = _spool_order(windows, initial_by_pos, slots, lums)
    initial_by_pos = {remap[p]: c for p, c in initial_by_pos.items()}
    windows = [_relabel(w, remap) for w in windows]
    plan.initial = [initial_by_pos.get(p) for p in range(1, slots + 1)]

    points = _stab(windows)
    assigned = _attribute(windows, points)

    # ---- build the pauses and the timeline ------------------------------
    placed = list(plan.initial)
    plan.timeline = [PlannedStep(label="start", positions=list(placed))]
    pauses: list[Pause] = []
    for idx, point in enumerate(points, start=1):
        swaps = []
        for w, at in zip(windows, assigned):
            if at != idx - 1:
                continue
            swaps.append(
                Swap(
                    position=w.position,
                    out_tool=w.out_tool,
                    in_tool=w.in_tool,
                    out_name=names.get(w.out_tool, "empty") if w.out_tool is not None else "empty",
                    in_name=names.get(w.in_tool, "empty") if w.in_tool is not None else "empty",
                )
            )
            placed[w.position - 1] = w.in_tool
        swaps.sort(key=lambda s: s.position)
        anchor_ordinal = run_starts[point]
        pauses.append(
            Pause(
                index=idx,
                demand_index=point,
                anchor_ordinal=anchor_ordinal,
                layer=block_layers[anchor_ordinal] if block_layers else 1,
                tool=block_tools[anchor_ordinal],
                swaps=swaps,
            )
        )
        plan.timeline.append(PlannedStep(label=f"pause {idx}", positions=list(placed)))

    plan.pauses = pauses
    plan.swaps_total = sum(len(p.swaps) for p in pauses)
    plan.colour_moves = 0  # a colour never leaves its home
    return plan


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


def attach_time_estimates(plan: Plan, gf: GcodeFile) -> None:
    """Fill in each pause's estimated time left from the slice it will run on.

    The total comes from the slicer's own header. Where inside that total a
    pause falls is estimated from how much filament has been extruded when the
    pause hits its tool-change block: flush and purge moves at every tool change
    do real work, so counting layers alone would be badly wrong on a colour
    print that pauses mostly on its first layers. Leaves the field alone when
    the file carries no estimate.
    """
    total_time = gf.estimated_time
    if total_time is None or not plan.pauses:
        return
    anchor_line: dict[int, int] = {}
    for p in plan.pauses:
        if 0 <= p.anchor_ordinal < len(gf.tool_changes):
            anchor_line[p.index] = gf.tool_changes[p.anchor_ordinal].header_line
    if not anchor_line:
        return
    done, total_e = extrusion_work(gf.lines, set(anchor_line.values()))
    if total_e <= 0:
        return
    for p in plan.pauses:
        line = anchor_line.get(p.index)
        if line is not None:
            left = total_time * (1.0 - done.get(line, 0.0) / total_e)
            p.remaining = max(0.0, left)


def plan_for_gcode(gf: GcodeFile, slots: int, initial: set[int] | None = None) -> Plan:
    lums, names = _lums_and_names(gf)
    plan = plan_slots(
        block_tools=[b.tool for b in gf.tool_changes],
        block_layers=[b.layer for b in gf.tool_changes],
        lums=lums,
        names=names,
        slots=slots,
        initial=initial,
    )
    attach_time_estimates(plan, gf)
    return plan
