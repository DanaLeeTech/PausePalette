"""Human-facing reports: plan tables, stage-2 instructions, print checklist."""

from __future__ import annotations

from .model import Plan


def initial_load_text(plan: Plan) -> str:
    parts = []
    for pos, tool in enumerate(plan.initial, start=1):
        if tool is None:
            continue
        parts.append(f"Pos{pos} {plan.tool_name(tool)}")
    return ", ".join(parts) if parts else "(none)"


def format_duration(seconds: float) -> str:
    """A print-time span in the shortest useful form: '2h 41m', '38m', '1d 4h'.

    Seconds are dropped — these are estimates, and minutes is all the detail the
    checklist needs. Anything under a minute reads '<1m'.
    """
    total = int(round(seconds))
    if total < 60:
        return "<1m"
    minutes = total // 60
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def homes(plan: Plan) -> dict[int, set[int]]:
    """position sets each colour occupies across the whole print."""
    out: dict[int, set[int]] = {}
    for step in plan.timeline:
        for pos, tool in enumerate(step.positions, start=1):
            if tool is not None:
                out.setdefault(tool, set()).add(pos)
    return out


def pause_rows(plan: Plan) -> list[dict]:
    rows = []
    for p in plan.pauses:
        rows.append(
            {
                "index": p.index,
                "layer": p.layer,
                "tool": p.tool,
                "tool_name": plan.tool_name(p.tool),
                "swaps": [s.describe() for s in p.swaps],
                "swaps_text": p.swap_summary(),
            }
        )
    return rows


def plan_markdown(plan: Plan) -> str:
    lines = [
        f"# Colour plan ({plan.slots} slots)",
        "",
        f"**Initial AMS load:** {initial_load_text(plan)}",
        "",
        f"**Pauses:** {len(plan.pauses)}  |  **Swaps:** {plan.swaps_total}  |  "
        f"**Position moves:** {plan.colour_moves}",
        "",
    ]
    if not plan.pauses:
        lines.append("_All colours fit in the AMS — no pauses needed._")
        return "\n".join(lines)

    lines += ["| # | Pause before | Layer | Swaps |", "|---|---|---|---|"]
    for p in plan.pauses:
        lines.append(
            f"| {p.index} | T{p.tool} {plan.tool_name(p.tool)} | {p.layer} | "
            f"{p.swap_summary()} |"
        )
    lines.append("")

    h = homes(plan)
    lines += ["## Positions", "", "| Colour | Position(s) |", "|---|---|"]
    for tool in sorted(h):
        positions = ", ".join(f"Pos{p}" for p in sorted(h[tool]))
        lines.append(f"| {plan.tool_name(tool)} | {positions} |")
    lines.append("")

    if plan.warnings:
        lines += ["## Warnings", ""]
        lines += [f"- {w}" for w in plan.warnings]
        lines.append("")
    return "\n".join(lines)


def stage2_instructions(plan: Plan, project_name: str) -> list[str]:
    names = plan.filament_name or plan.expected_filament_order
    order = "; ".join(f"{i + 1}. {n}" for i, n in enumerate(names))
    steps = [
        f"Open the downloaded reordered project in Bambu Studio (the file for '{project_name}').",
    ]
    if plan.layer_orders:
        steps.append(
            "Don't change the filament list or the plate's filament sequence — the "
            "plan is already set in the project."
        )
    else:
        steps.append(f"Set the filament list to exactly: {order}")
    if plan.printer_model:
        steps.append(f"Confirm the printer is '{plan.printer_model}' with its AMS.")
    steps += [
        "Slice the plate.",
        "Export the plate sliced file (.gcode or .3mf).",
        "Upload that file back into this job.",
    ]
    return steps


def checklist_markdown(plan: Plan, project_name: str, printer_model: str | None) -> str:
    lines = [
        f"# PausePalette swap checklist — {project_name}",
        "",
        "> **Print from file. Never re-slice this file.**",
        "> The technique is validated, but this specific file has not been printed —",
        "> skim the pause table before you start.",
        "",
        f"- Printer: {printer_model or 'unknown'}",
        f"- AMS slots: {plan.slots}",
        f"- Pauses: {len(plan.pauses)}",
        f"- Swaps: {plan.swaps_total}",
        "",
        "## Before you start — load the AMS",
        "",
        f"**{initial_load_text(plan)}**",
        "",
    ]
    if not plan.pauses:
        lines.append("No pauses are needed for this print.")
        return "\n".join(lines)

    for p in plan.pauses:
        left = f" - {p.remaining_text}" if p.remaining_text else ""
        lines += [
            f"## Pause {p.index} of {len(plan.pauses)} — layer {p.layer}{left}, before "
            f"T{p.tool} ({plan.tool_name(p.tool)})",
            "",
        ]
        for s in p.swaps:
            lines.append(f"- [ ] {s.describe()}")
        lines.append("")

    h = homes(plan)
    lines += ["## Positions used", ""]
    for tool in sorted(h):
        positions = ", ".join(f"Pos{p}" for p in sorted(h[tool]))
        lines.append(f"- {plan.tool_name(tool)}: {positions}")
    lines.append("")
    return "\n".join(lines)
