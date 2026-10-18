"""PausePalette web app — the two-stage flow.

    stage 1  upload a .3mf project  -> plan + reordered project to download
    stage 2  (reorder / slice / export in Bambu Studio — outside the app)
    stage 3  upload the slice back  -> paused G-code + checklist to download

A single-stage shortcut is offered for files that are already sliced and already
in their final filament order.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pausepalette import report, threemf, uploadcheck
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
    "output.3mf",
    "checklist.md",
    "report.json",
}

MAX_BYTES = 400 * 1024 * 1024

#: jobs are kept for this long after they were last touched
JOB_TTL_DAYS = 5
JOB_TTL_SECONDS = JOB_TTL_DAYS * 24 * 3600

#: the per-browser token that lets jobs follow you to another network
CLIENT_COOKIE = "pp_client"

#: what the machine running PausePalette is called in the job list
LOCAL_CLIENT = "local"

#: hops allowed to report the client's address (a reverse proxy in front):
#: a comma-separated list of addresses/CIDRs, or "*" to trust any hop. By
#: default only loopback and private addresses are trusted, which covers a
#: proxy on this machine or the LAN but not one out on the internet.
TRUSTED_PROXIES = os.environ.get("PAUSEPALETTE_TRUSTED_PROXIES", "").strip()

#: the public address the app is served at when a proxy fronts it, e.g.
#: https://pausepalette.example.com. Optional: it is what the pages advertise
#: as their canonical address and what link previews point at. Cookie security
#: is not taken from here — that follows the actual request — so a public
#: address never stops the app working when it is reached directly over plain
#: HTTP.
PUBLIC_URL = os.environ.get("PAUSEPALETTE_PUBLIC_URL", "").strip().rstrip("/")


def _trusted_networks():
    """The trusted-proxy setting, parsed: None = default, True = any hop."""
    if TRUSTED_PROXIES == "*":
        return True
    nets = []
    for item in TRUSTED_PROXIES.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return nets


_TRUSTED = _trusted_networks()


def _default_gateway() -> str:
    """This machine's default gateway address.

    In Docker that is the bridge address: the port proxy forwards connections
    from the host itself through it, so every browser on the host looks like the
    gateway rather than like itself.
    """
    try:
        with open("/proc/net/route", encoding="ascii") as fh:
            for line in fh.readlines()[1:]:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    hexip = fields[2]          # little-endian hex
                    return ".".join(
                        str(int(hexip[i : i + 2], 16)) for i in (6, 4, 2, 0)
                    )
    except OSError:  # pragma: no cover - not Linux
        pass
    return ""


_LOCAL_GATEWAY = _default_gateway()


def _is_local(address: str) -> bool:
    """Loopback, or the gateway the host's own connections arrive through."""
    if not address:
        return False
    return address in ("127.0.0.1", "::1", "localhost") or address == _LOCAL_GATEWAY


def _can_forward(request: Request) -> bool:
    """Whether this hop is allowed to tell us the client's address.

    A hop is trusted if it is named in ``PAUSEPALETTE_TRUSTED_PROXIES``, or —
    by default — if it is already local or private (a proxy on this machine,
    the LAN or a VPN). A client out on the internet is never trusted, so it
    cannot claim somebody else's address with a header.
    """
    host = request.client.host if request.client else ""
    if not host:
        return False
    if _TRUSTED is True:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True            # not an address at all: a local transport
    if any(ip in net for net in _TRUSTED):
        return True
    return ip.is_loopback or ip.is_private


def _forwarded(request: Request, header: str) -> str:
    """The first value of a forwarding header, when the hop may set it."""
    if not _can_forward(request):
        return ""
    return request.headers.get(header, "").split(",")[0].strip()


def _client_ip(request: Request) -> str:
    if _can_forward(request):
        # Cloudflare overwrites this one itself, so it stays right even when
        # several visitors share the hop the proxy connects from
        connecting = request.headers.get("cf-connecting-ip", "").strip()
        if connecting:
            return connecting
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real = request.headers.get("x-real-ip", "")
        if real:
            return real.strip()
    host = request.client.host if request.client else ""
    return LOCAL_CLIENT if _is_local(host) else host


def _is_https(request: Request) -> bool:
    """Whether the client reached us over TLS, trusting a proxy's word for it."""
    proto = _forwarded(request, "x-forwarded-proto")
    if proto:
        return proto.lower() == "https"
    return request.url.scheme == "https"


def _site_url(request: Request) -> str:
    """The address the pages should advertise: the public one, if configured."""
    if PUBLIC_URL:
        return PUBLIC_URL
    scheme = "https" if _is_https(request) else request.url.scheme
    host = _forwarded(request, "x-forwarded-host") or request.headers.get("host", "")
    return f"{scheme}://{host}" if host else ""


#: templates advertise the canonical address (and link previews) from here
templates.env.globals["site_url"] = _site_url


@app.middleware("http")
async def identify_client(request: Request, call_next):
    """Give every browser a token, so its jobs can follow it anywhere."""
    token = request.cookies.get(CLIENT_COOKIE)
    fresh = token is None
    if fresh:
        token = uuid.uuid4().hex
    request.state.client_id = token
    request.state.client_ip = _client_ip(request)
    response = await call_next(request)
    if fresh:
        # browsers cap cookie life at 400 days; Secure once the public site is
        # served over TLS, so the token cannot leak on a plain-HTTP hop
        response.set_cookie(
            CLIENT_COOKIE,
            token,
            max_age=400 * 24 * 3600,
            httponly=True,
            samesite="lax",
            secure=_is_https(request),
        )
    return response


def _client_identity(request: Request) -> dict:
    return {
        "client_ip": getattr(request.state, "client_ip", ""),
        "client_id": getattr(request.state, "client_id", ""),
    }


def _recent_jobs(request: Request, limit: int = 12) -> list[dict]:
    """This client's jobs: the same IP *or* the same browser, minus expired ones.

    Either match is enough, so a job is still there from another browser on the
    same network and from the same browser on another network. Jobs made before
    PausePalette recorded any of this are shown to everyone.
    """
    store.prune(JOB_TTL_SECONDS)
    client = _client_identity(request)
    out: list[dict] = []
    for job in store.list():
        job_ip = job.get("client_ip", "")
        job_id = job.get("client_id", "")
        if not job_ip and not job_id:
            out.append(job)                       # predates the client record
        elif (client["client_ip"] and job_ip == client["client_ip"]) or (
            client["client_id"] and job_id == client["client_id"]
        ):
            out.append(job)
        if len(out) >= limit:
            break
    return out


def _safe_name(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9 ._-]+", "_", Path(name).name).strip() or "file"
    return base[:120]


def _title_from(name: str) -> str:
    return Path(_safe_name(name)).stem or "project"


async def _read_upload(file: UploadFile, kind: str) -> bytes:
    """Read an upload and refuse anything that is not what it claims to be.

    ``kind`` is ``"project"`` for the stage-1 form or ``"slice"`` for the
    sliced-file forms. The check runs before a job is created, so a rejected
    file never leaves anything behind.
    """
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise StopAndExplain(f"{file.filename} is larger than the {MAX_BYTES // (1024*1024)} MB limit")
    if not data:
        raise StopAndExplain(f"{file.filename} is empty")
    if kind == "project":
        uploadcheck.check_project(file.filename or "", data)
    else:
        uploadcheck.check_slice(file.filename or "", data)
    return data


@app.get("/", response_class=HTMLResponse)
def index(request: Request, error: str | None = None):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"jobs": _recent_jobs(request), "error": error, "slots": 4, "plate": 1,
         "ttl_days": JOB_TTL_DAYS},
    )


@app.post("/plan")
async def plan(
    request: Request,
    project: UploadFile = File(...),
    slots: int = Form(4),
    plate: int = Form(1),
):
    try:
        data = await _read_upload(project, "project")
    except StopAndExplain as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"jobs": _recent_jobs(request), "error": str(exc), "slots": slots,
             "plate": plate, "ttl_days": JOB_TTL_DAYS},
            status_code=400,
        )

    filename = project.filename or "project.3mf"
    job_id = store.create(_title_from(filename))
    try:
        _run_project_plan(job_id, filename, data, slots, plate, **_client_identity(request))
    except StopAndExplain as exc:
        store.delete(job_id)  # don't leave a half-made job behind
        return templates.TemplateResponse(
            request,
            "index.html",
            {"jobs": _recent_jobs(request), "error": str(exc), "slots": slots,
             "plate": plate, "ttl_days": JOB_TTL_DAYS},
            status_code=400,
        )
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.post("/convert")
async def convert(
    request: Request,
    slice_file: UploadFile = File(...),
    slots: int = Form(4),
    plate: int | None = Form(None),
):
    try:
        data = await _read_upload(slice_file, "slice")
    except StopAndExplain as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"jobs": _recent_jobs(request), "error": str(exc), "slots": slots,
             "ttl_days": JOB_TTL_DAYS},
            status_code=400,
        )

    filename = slice_file.filename or "slice.gcode"
    job_id = store.create(_title_from(filename))
    try:
        _run_convert(job_id, filename, data, slots, plate, **_client_identity(request))
    except StopAndExplain as exc:
        store.delete(job_id)
        return templates.TemplateResponse(
            request,
            "index.html",
            {"jobs": _recent_jobs(request), "error": str(exc), "slots": slots,
             "ttl_days": JOB_TTL_DAYS},
            status_code=400,
        )
    return RedirectResponse(f"/job/{job_id}", status_code=303)


# --- re-running and removing a job -----------------------------------------

# everything that belongs to a slice round-trip; a fresh plan invalidates it
STAGE3_ARTEFACTS = (
    "output.gcode",
    "output.3mf",
    "checklist.md",
    "report.json",
    "slice_input.gcode",
    "slice_input.3mf",
)


def _client_or_existing(job_id: str, client_ip: str | None, client_id: str | None) -> dict:
    """The client to record: the caller's, or whatever the job already had."""
    if client_ip is not None and client_id is not None:
        return {"client_ip": client_ip, "client_id": client_id}
    try:
        old = store.meta(job_id)
    except (OSError, ValueError):
        old = {}
    return {
        "client_ip": client_ip if client_ip is not None else old.get("client_ip", ""),
        "client_id": client_id if client_id is not None else old.get("client_id", ""),
    }


def _run_project_plan(job_id: str, filename: str, data: bytes, slots: int, plate: int,
                      first_colour: int | None = None,
                      client_ip: str | None = None, client_id: str | None = None) -> None:
    """Stage 1 — plan a project and write the reordered copy."""
    stage1 = plan_from_project(data, slots=slots, plate=plate, first_colour=first_colour)
    store.save(job_id, "project.3mf", data)
    store.save(job_id, "reordered.3mf", stage1.reordered_project)
    store.write_json(job_id, "plan.json", plan_to_dict(stage1.plan))
    store.save(job_id, "plan.md", report.plan_markdown(stage1.plan))
    for name in STAGE3_ARTEFACTS:
        store.remove(job_id, name)
    store.reset_meta(
        job_id,
        **_client_or_existing(job_id, client_ip, client_id),
        stage="planned",
        original_name=_safe_name(filename or "project.3mf"),
        slots=slots,
        plate=stage1.plate,
        plates=stage1.plates,
        printer_model=stage1.printer_model,
        filaments=stage1.original_filaments,
        filament_names=stage1.original_names,
        new_order=stage1.plan.filament_colour,
        pauses=len(stage1.plan.pauses),
        swaps=stage1.plan.swaps_total,
        moves=stage1.plan.colour_moves,
        provisional=stage1.plan.provisional,
        deferred=stage1.plan.deferred,
        first_colour=stage1.plan.first_colour,
        first_colour_choices=stage1.plan.first_colour_choices,
        reorder_report={
            "json_keys_permuted": stage1.reorder_report.json_keys_permuted,
            "references_remapped": stage1.reorder_report.references_remapped,
            "slices_stripped": stage1.reorder_report.slices_stripped,
            "sequence_written": stage1.reorder_report.sequence_written,
            "sequence_layers": stage1.reorder_report.sequence_layers,
            "warnings": stage1.reorder_report.warnings,
        },
    )


def _run_convert(job_id: str, filename: str, data: bytes,
                 slots: int, plate: int | None,
                 client_ip: str | None = None, client_id: str | None = None) -> None:
    """Single stage — insert the pauses into an already sliced file."""
    result = plan_and_insert(data, slots=slots, plate=plate)
    store.save(
        job_id,
        "slice_input.3mf" if threemf.looks_like_zip(data) else "slice_input.gcode",
        data,
    )
    store.write_json(job_id, "plan.json", plan_to_dict(result.plan))
    store.save(job_id, "plan.md", report.plan_markdown(result.plan))
    meta = {
        **_client_or_existing(job_id, client_ip, client_id),
        "stage": "converted",
        "original_name": _safe_name(filename or "slice.gcode"),
        "slots": slots,
        "plate": result.plan.plate if result.plan else None,
        "printer_model": result.plan.printer_model,
        "pauses": result.edited.inserted_blocks,
        "swaps": result.plan.swaps_total,
        "moves": result.plan.colour_moves,
    }
    _finish_job(job_id, result, meta, plan=result.plan)


@app.post("/job/{job_id}/regenerate")
def regenerate(job_id: str):
    """Re-run the job from its stored input, exactly as if it were re-uploaded."""
    try:
        meta = store.meta(job_id)
    except (OSError, ValueError):
        return RedirectResponse("/", status_code=303)

    slots = int(meta.get("slots") or 4)
    name = meta.get("original_name") or "upload"
    try:
        if store.exists(job_id, "project.3mf"):
            _run_project_plan(job_id, name, store.load(job_id, "project.3mf"),
                              slots, meta.get("plate") or 1,
                              first_colour=meta.get("first_colour"))
        elif store.exists(job_id, "slice_input.3mf"):
            _run_convert(job_id, name, store.load(job_id, "slice_input.3mf"),
                         slots, meta.get("plate"))
        elif store.exists(job_id, "slice_input.gcode"):
            _run_convert(job_id, name, store.load(job_id, "slice_input.gcode"),
                         slots, meta.get("plate"))
        else:
            raise StopAndExplain("this job has no stored input left to re-plan from")
    except StopAndExplain as exc:
        return RedirectResponse(f"/job/{job_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.post("/job/{job_id}/first-colour")
def set_first_colour(job_id: str, first_colour: int = Form(...)):
    """Re-plan the stored project starting with the colour the user picked."""
    try:
        meta = store.meta(job_id)
    except (OSError, ValueError):
        return RedirectResponse("/", status_code=303)
    if not store.exists(job_id, "project.3mf"):
        return RedirectResponse(
            f"/job/{job_id}?error={quote('this job has no stored project to re-plan')}",
            status_code=303,
        )
    choices = meta.get("first_colour_choices") or []
    if choices and first_colour not in choices:
        message = "that colour is not one of the first layer's colours"
        return RedirectResponse(f"/job/{job_id}?error={quote(message)}", status_code=303)
    try:
        _run_project_plan(
            job_id,
            meta.get("original_name") or "project.3mf",
            store.load(job_id, "project.3mf"),
            int(meta.get("slots") or 4),
            meta.get("plate") or 1,
            first_colour=first_colour,
            **_client_or_existing(job_id, None, None),
        )
    except StopAndExplain as exc:
        return RedirectResponse(f"/job/{job_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.post("/job/{job_id}/delete")
def delete_job(job_id: str):
    try:
        store.delete(job_id)
    except (OSError, ValueError):
        pass
    return RedirectResponse("/", status_code=303)


@app.post("/job/{job_id}/slice")
async def upload_slice(request: Request, job_id: str, slice_file: UploadFile = File(...)):
    try:
        plan = plan_from_dict(store.read_json(job_id, "plan.json"))
        data = await _read_upload(slice_file, "slice")
        result = insert_into_slice(data, plan)
    except StopAndExplain as exc:
        return _job_page(request, job_id, error=str(exc), status_code=400)

    store.save(job_id, "slice_input.3mf" if threemf.looks_like_zip(data) else "slice_input.gcode", data)
    meta = {"stage": "applied", "slice_name": _safe_name(slice_file.filename or "slice")}
    if result.plan is not None:
        # the slice finalises a provisional/deferred plan: keep the resolved one
        # so the page and checklist show the pauses that were actually inserted
        plan = result.plan
        store.write_json(job_id, "plan.json", plan_to_dict(plan))
        store.save(job_id, "plan.md", report.plan_markdown(plan))
        meta.update(
            pauses=len(plan.pauses),
            swaps=plan.swaps_total,
            provisional=plan.provisional,
            deferred=plan.deferred,
        )
    _finish_job(job_id, result, meta, plan=plan)
    return RedirectResponse(f"/job/{job_id}", status_code=303)


def _finish_job(job_id: str, result, meta: dict, plan=None) -> None:
    text = result.edited.text
    stored = store.meta(job_id)
    name = meta.get("original_name") or stored.get("original_name") or "print"
    base = re.sub(r"\.gcode$", "", Path(name).stem)   # 'x.gcode.3mf' -> 'x'
    store.save(job_id, "output.gcode", text)
    # a sliced project goes back out as a sliced project: the same .3mf with the
    # edited G-code (and its checksum) in place, so Studio prints it as it is
    has_output_3mf = bool(result.slice_name) and store.exists(job_id, "slice_input.3mf")
    if has_output_3mf:
        store.save(
            job_id,
            "output.3mf",
            threemf.repack_slice(
                store.load(job_id, "slice_input.3mf"), text, result.slice_name
            ),
        )
    else:
        store.remove(job_id, "output.3mf")
    # name the downloads after the plate they are for: the plate whose slice was
    # edited when we know it, otherwise the plate the plan was made for
    sliced = re.match(r"Metadata/plate_(\d+)\.gcode$", result.slice_name or "")
    slice_plate = int(sliced.group(1)) if sliced else None
    plate = slice_plate or meta.get("plate") or stored.get("plate")
    suffix = f"_plate{plate}" if plate else ""
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
        output_name=f"{base}_pauses{suffix}.gcode",
        slice_plate=slice_plate,
        has_output_3mf=has_output_3mf,
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
    store.prune(JOB_TTL_SECONDS)   # keep the same window the list promises
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
    homes = report.homes(plan) if plan is not None else None
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
            "homes": homes,
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
    base = re.sub(r"\.gcode$", "", Path(meta.get("original_name", "print")).stem)
    plate = meta.get("plate")
    output_name = meta.get("output_name", f"{base}_pauses.gcode")
    download_name = {
        "reordered.3mf": f"{base}_pausepalette{f'_plate{plate}' if plate else ''}.3mf",
        "output.gcode": output_name,
        "output.3mf": f"{output_name}.3mf",
    }.get(name, name)
    return FileResponse(path, filename=download_name)


@app.get("/healthz")
def healthz():
    return {"ok": True, "jobs": len(store.list())}
