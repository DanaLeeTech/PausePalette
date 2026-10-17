"""PausePalette web app — the two-stage flow.

    stage 1  upload a .3mf project  -> plan + reordered project to download
    stage 2  (reorder / slice / export in Bambu Studio — outside the app)
    stage 3  upload the slice back  -> paused G-code + checklist to download

A single-stage shortcut is offered for files that are already sliced and already
in their final filament order.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pausepalette import report, threemf
from pausepalette.errors import StopAndExplain
from pausepalette.jobstore import JobStore
from pausepalette.pipeline import (
    insert_into_slice,
    plan_and_insert,
    plan_from_dict,
    plan_from_project,
    plan_to_dict,
)

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PAUSEPALETTE_DATA", HERE.parent / "data" / "jobs"))
store = JobStore(DATA_DIR)

app = FastAPI(title="PausePalette", version="0.1.0")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))

DOWNLOAD_WHITELIST = {
    "project.3mf",
    "reordered.3mf",
    "plan.json",
    "plan.md",
    "slice_input.gcode",
    "slice_input.3mf",
    "output.gcode",
    "checklist.md",
    "report.json",
}

MAX_BYTES = 400 * 1024 * 1024


def _safe_name(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9 ._-]+", "_", Path(name).name).strip() or "file"
    return base[:120]


def _title_from(name: str) -> str:
    return Path(_safe_name(name)).stem or "project"


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise StopAndExplain(f"{file.filename} is larger than the {MAX_BYTES // (1024*1024)} MB limit")
    if not data:
        raise StopAndExplain(f"{file.filename} is empty")
    return data


@app.get("/", response_class=HTMLResponse)
def index(request: Request, error: str | None = None):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"jobs": store.list()[:12], "error": error, "slots": 4},
    )


@app.post("/plan")
async def plan(
    request: Request,
    project: UploadFile = File(...),
    slots: int = Form(4),
):
    try:
        data = await _read_upload(project)
        stage1 = plan_from_project(data, slots=slots)
    except StopAndExplain as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"jobs": store.list()[:12], "error": str(exc), "slots": slots},
            status_code=400,
        )

    title = _title_from(project.filename or "project.3mf")
    job_id = store.create(title)
    store.save(job_id, "project.3mf", data)
    store.save(job_id, "reordered.3mf", stage1.reordered_project)
    store.write_json(job_id, "plan.json", plan_to_dict(stage1.plan))
    store.save(job_id, "plan.md", report.plan_markdown(stage1.plan))
    store.update_meta(
        job_id,
        stage="planned",
        original_name=_safe_name(project.filename or "project.3mf"),
        slots=slots,
        printer_model=stage1.printer_model,
        filaments=stage1.original_filaments,
        filament_names=stage1.original_names,
        new_order=stage1.plan.filament_colour,
        pauses=len(stage1.plan.pauses),
        swaps=stage1.plan.swaps_total,
        moves=stage1.plan.colour_moves,
        notes=stage1.notes,
        reorder_report={
            "json_keys_permuted": stage1.reorder_report.json_keys_permuted,
            "references_remapped": stage1.reorder_report.references_remapped,
            "slices_stripped": stage1.reorder_report.slices_stripped,
            "warnings": stage1.reorder_report.warnings,
        },
    )
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.post("/convert")
async def convert(request: Request, slice_file: UploadFile = File(...), slots: int = Form(4)):
    try:
        data = await _read_upload(slice_file)
        result = plan_and_insert(data, slots=slots)
    except StopAndExplain as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"jobs": store.list()[:12], "error": str(exc), "slots": slots},
            status_code=400,
        )

    title = _title_from(slice_file.filename or "slice.gcode")
    job_id = store.create(title)
    store.write_json(job_id, "plan.json", plan_to_dict(result.plan))
    store.save(job_id, "plan.md", report.plan_markdown(result.plan))
    meta = {
        "stage": "converted",
        "original_name": _safe_name(slice_file.filename or "slice.gcode"),
        "slots": slots,
        "printer_model": result.plan.printer_model,
        "pauses": result.edited.inserted_blocks,
        "swaps": result.plan.swaps_total,
        "moves": result.plan.colour_moves,
    }
    _finish_job(job_id, result, meta, plan=result.plan)
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.post("/job/{job_id}/slice")
async def upload_slice(request: Request, job_id: str, slice_file: UploadFile = File(...)):
    try:
        plan = plan_from_dict(store.read_json(job_id, "plan.json"))
        data = await _read_upload(slice_file)
        result = insert_into_slice(data, plan)
    except StopAndExplain as exc:
        return _job_page(request, job_id, error=str(exc), status_code=400)

    store.save(job_id, "slice_input.3mf" if threemf.looks_like_zip(data) else "slice_input.gcode", data)
    meta = {"stage": "applied", "slice_name": _safe_name(slice_file.filename or "slice")}
    _finish_job(job_id, result, meta, plan=plan)
    return RedirectResponse(f"/job/{job_id}", status_code=303)


def _finish_job(job_id: str, result, meta: dict, plan=None) -> None:
    text = result.edited.text
    base = store.meta(job_id).get("original_name", "print")
    base = Path(base).stem
    store.save(job_id, "output.gcode", text)
    if plan is None and store.exists(job_id, "plan.json"):
        plan = plan_from_dict(store.read_json(job_id, "plan.json"))
    checklist = ""
    if plan is not None:
        checklist = report.checklist_markdown(
            plan, base, plan.printer_model or store.meta(job_id).get("printer_model")
        )
    store.save(job_id, "checklist.md", checklist)
    checks = [{"name": n, "ok": ok, "detail": d} for (n, ok, d) in result.report.checks]
    store.write_json(job_id, "report.json", {"ok": result.report.ok, "checks": checks})
    store.update_meta(
        job_id,
        **meta,
        output_name=f"{base}_pauses.gcode",
        inserted=result.edited.inserted_blocks,
        edited_blocks=result.edited.edited_blocks,
        lines_inserted=result.edited.lines_inserted,
        verification_ok=result.report.ok,
        notes=list(getattr(result, "notes", [])),
    )


@app.get("/job/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str, error: str | None = None):
    return _job_page(request, job_id, error=error)


def _job_page(request: Request, job_id: str, error: str | None = None, status_code: int = 200):
    try:
        meta = store.meta(job_id)
    except (OSError, ValueError):
        return templates.TemplateResponse(
            request,
            "error.html",
            {"error": f"no such job: {job_id}"},
            status_code=404,
        )
    plan_md = store.load_text(job_id, "plan.md") if store.exists(job_id, "plan.md") else ""
    plan = None
    if store.exists(job_id, "plan.json"):
        plan = plan_from_dict(store.read_json(job_id, "plan.json"))
    report_data = store.read_json(job_id, "report.json") if store.exists(job_id, "report.json") else None
    checklist = store.load_text(job_id, "checklist.md") if store.exists(job_id, "checklist.md") else ""
    instructions = report.stage2_instructions(plan, meta.get("original_name", "")) if plan else []
    return templates.TemplateResponse(
        request,
        "job.html",
        {
            "job": meta,
            "plan": plan,
            "plan_md": plan_md,
            "report": report_data,
            "checklist": checklist,
            "instructions": instructions,
            "error": error,
        },
        status_code=status_code,
    )


@app.get("/job/{job_id}/download/{name}")
def download(job_id: str, name: str):
    if name not in DOWNLOAD_WHITELIST:
        return JSONResponse({"error": "not allowed"}, status_code=403)
    path = store.path(job_id, name)
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    meta = store.meta(job_id)
    base = Path(meta.get("original_name", "print")).stem
    download_name = {
        "reordered.3mf": f"{base}_pausepalette.3mf",
        "output.gcode": meta.get("output_name", f"{base}_pauses.gcode"),
    }.get(name, name)
    return FileResponse(path, filename=download_name)


@app.get("/healthz")
def healthz():
    return {"ok": True, "jobs": len(store.list())}
