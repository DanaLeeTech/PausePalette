"""PausePalette command line.

    pausepalette plan project.3mf --slots 4 -o project_pp.3mf --plan plan.json
    pausepalette apply slice.gcode --plan plan.json -o slice_pauses.gcode
    pausepalette convert slice.gcode --slots 4 -o slice_pauses.gcode   # single stage
    pausepalette analyze slice.gcode
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import report, threemf
from .errors import StopAndExplain
from .gcode import analysis_summary, parse_gcode
from .pipeline import (
    insert_into_slice,
    plan_and_insert,
    plan_from_dict,
    plan_from_project,
    plan_to_dict,
)
from .planner import plan_for_gcode


def _out_path(src: Path, suffix: str) -> Path:
    return src.with_name(f"{src.stem}{suffix}{src.suffix}" if src.suffix else f"{src.stem}{suffix}")


def cmd_analyze(args: argparse.Namespace) -> int:
    src = Path(args.input)
    data = src.read_bytes()
    text = None
    if threemf.looks_like_zip(data):
        info = threemf.read_project(data)
        print(f"project: {src.name}")
        print(f"  printer: {info.printer_model}")
        print(f"  filaments: {';'.join(info.filaments)}")
        print(f"  embedded slice: {info.has_slice} {info.slice_names}")
        text = info.slice_gcode
        if text is None:
            return 0
    else:
        text = data.decode("utf-8")
    summary = analysis_summary(parse_gcode(text))
    print(json.dumps(summary, indent=2))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    src = Path(args.input)
    result = plan_from_project(src.read_bytes(), slots=args.slots, plate=args.plate)
    out_proj = Path(args.output) if args.output else _out_path(src, "_pausepalette")
    out_proj.write_bytes(result.reordered_project)
    plan_path = Path(args.plan) if args.plan else _out_path(out_proj, ".plan").with_suffix(".plan.json")
    plan_path.write_text(json.dumps(plan_to_dict(result.plan), indent=2), encoding="utf-8")

    print(report.plan_markdown(result.plan))
    print(f"reordered project: {out_proj}")
    print(f"plan:              {plan_path}")
    print()
    print("In your slicer:")
    for i, s in enumerate(report.stage2_instructions(result.plan, src.name), 1):
        print(f"  {i}. {s}")
    for n in result.notes:
        print(f"note: {n}")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    plan = plan_from_dict(json.loads(Path(args.plan).read_text(encoding="utf-8")))
    src = Path(args.input)
    result = insert_into_slice(src.read_bytes(), plan)
    out = Path(args.output) if args.output else _out_path(src, "_pauses")
    out.write_bytes(result.edited.text.encode("utf-8"))

    checklist = out.with_suffix(".checklist.md")
    checklist.write_text(
        report.checklist_markdown(plan, src.name, plan.printer_model), encoding="utf-8"
    )
    print(f"output:    {out}")
    print(f"checklist: {checklist}")
    print(f"inserted {result.edited.inserted_blocks} pause blocks "
          f"({result.edited.lines_inserted} lines), edited {result.edited.edited_blocks} blocks")
    for name, ok, detail in result.report.checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    for n in result.notes:
        print(f"note: {n}")
    return 0 if result.report.ok else 1


def cmd_convert(args: argparse.Namespace) -> int:
    src = Path(args.input)
    result = plan_and_insert(src.read_bytes(), slots=args.slots, plate=args.plate)
    out = Path(args.output) if args.output else _out_path(src, "_pauses")
    out.write_bytes(result.edited.text.encode("utf-8"))
    checklist = out.with_suffix(".checklist.md")
    checklist.write_text(
        report.checklist_markdown(
            result.plan, src.name, result.plan.printer_model if result.plan else None
        ),
        encoding="utf-8",
    )
    print(f"output:    {out}")
    print(f"checklist: {checklist}")
    print(f"inserted {result.edited.inserted_blocks} pause blocks")
    for name, ok, detail in result.report.checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return 0 if result.report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pausepalette", description="Offline colour planning and pause insertion for AMS prints")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("analyze", help="analyse a project or sliced file")
    a.add_argument("input")
    a.set_defaults(func=cmd_analyze)

    pl = sub.add_parser("plan", help="stage 1: plan from a 3mf project and write a reordered project")
    pl.add_argument("input")
    pl.add_argument("-o", "--output", help="reordered project path")
    pl.add_argument("--plan", help="plan json path")
    pl.add_argument("--slots", type=int, default=4)
    pl.add_argument("--plate", type=int, default=1, help="plate number to plan (default 1)")
    pl.set_defaults(func=cmd_plan)

    ap = sub.add_parser("apply", help="stage 3: insert pauses from a plan into a sliced file")
    ap.add_argument("input")
    ap.add_argument("--plan", required=True)
    ap.add_argument("-o", "--output")
    ap.set_defaults(func=cmd_apply)

    cv = sub.add_parser("convert", help="single stage: plan and insert pauses into a sliced file")
    cv.add_argument("input")
    cv.add_argument("-o", "--output")
    cv.add_argument("--slots", type=int, default=4)
    cv.add_argument("--plate", type=int, default=None, help="plate number inside a 3mf")
    cv.set_defaults(func=cmd_convert)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except StopAndExplain as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
