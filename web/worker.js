/* PausePalette Pyodide worker, v2 — buffer-id protocol, jobs in IndexedDB.
 *
 * Main -> worker (every message carries `id`; the reply repeats it):
 *   {id, type:"init"}
 *   {id, type:"plan", filename, slots, plate, firstColour?, file:ArrayBuffer}
 *   {id, type:"convert", filename, slots, file:ArrayBuffer}
 *   {id, type:"slice", jobId, filename, file:ArrayBuffer}
 *   {id, type:"regenerate", jobId}
 *   {id, type:"firstColour", jobId, colour}
 *   {id, type:"readBuffer", jobId, name}
 *   {id, type:"listJobs"}          {id, type:"getJob", jobId}
 *   {id, type:"deleteJob", jobId}  {id, type:"clearJobs"}
 *
 * Replies are plain JSON ({id, ok:true, ...} or {id, ok:false, error, ...})
 * plus transferred ArrayBuffers. File bytes never travel inside JSON: uploads
 * arrive as a transferred ArrayBuffer and downloads come from readBuffer.
 */

"use strict";

importScripts("store.js");

const store = self.PausePaletteStore;

// 0.29.5 is the newest stable Pyodide that still supports classic workers;
// 314.x throws "Classic web workers are not supported" at importScripts.
const PYODIDE_VERSION = "0.29.5";
const PYODIDE_URL = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;

// Everything under pausepalette/ and pausepalette/dialects/ except the CLI and
// the server-side job store. Listed explicitly so a new module is a deliberate
// addition, not a silent 404.
const CORE_FILES = [
  "pausepalette/__init__.py",
  "pausepalette/colours.py",
  "pausepalette/editor.py",
  "pausepalette/errors.py",
  "pausepalette/gcode.py",
  "pausepalette/model.py",
  "pausepalette/pipeline.py",
  "pausepalette/planner.py",
  "pausepalette/report.py",
  "pausepalette/threemf.py",
  "pausepalette/uploadcheck.py",
  "pausepalette/verifier.py",
  "pausepalette/dialects/__init__.py",
  "pausepalette/dialects/bambu.py",
  "pausepalette/dialects/bambu_models.py",
  "pausepalette/dialects/generic.py",
  "pausepalette/dialects/printer_pauses.py",
];

// The app is always served from the built dist/, with the core beside it.
const CORE_BASE = new URL("./", self.location.href).href;

// Only these may be read back out of the job store for download.
const DOWNLOAD_WHITELIST = new Set([
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
]);

// Everything that belongs to a slice round-trip; a fresh plan invalidates it.
const STAGE3_ARTEFACTS = [
  "output.gcode",
  "output.3mf",
  "checklist.md",
  "report.json",
  "slice_input.gcode",
  "slice_input.3mf",
];

const MIME = {
  json: "application/json",
  md: "text/markdown",
  gcode: "text/plain",
  "3mf": "application/zip",
};

let pyodide = null;
let PY = null;
let bootInfo = null;
let booting = null;

// Ingestion handles for the call in flight: id -> {proxy, bytes, label}.
// Nothing here outlives a message, so the Python heap does not grow.
const buffers = new Map();
let bufferSeq = 0;

function now() {
  return performance.now();
}

function nowSec() {
  return Date.now() / 1000;
}

function round(ms) {
  return Math.round(ms);
}

function bufferDiag() {
  let bytes = 0;
  for (const entry of buffers.values()) bytes += entry.bytes;
  return { count: buffers.size, bytes: bytes };
}

function diag() {
  return { buffers: bufferDiag(), storage: store.mode };
}

function resources() {
  return performance.getEntriesByType("resource").map((e) => ({
    name: e.name.split("/").slice(-2).join("/"),
    transferSize: e.transferSize,
    decodedBodySize: e.decodedBodySize,
  }));
}

/* The core, wrapped so a StopAndExplain reaches the UI as its plain sentence
 * rather than as a Pyodide traceback. Every function returns JSON: app.py
 * wrote the same structures with json.dumps, and JSON has no `undefined` — the
 * Pyodide None -> undefined quirk cannot drop keys on the way out. Big bytes
 * are parked in `_parked` and collected with take(), never JSON-encoded. */
const PY_SRC = `
import json

from pausepalette import report, threemf, uploadcheck
from pausepalette.errors import StopAndExplain
from pausepalette.pipeline import (
    insert_into_slice,
    plan_and_insert,
    plan_from_dict,
    plan_from_project,
    plan_to_dict,
)

MAX_BYTES = 400 * 1024 * 1024

_parked = {}


def _json(obj):
    return json.dumps(obj, ensure_ascii=False)


def _park(name, value):
    _parked[name] = value
    return len(value)


def take(name):
    """Hand parked bytes to JS and drop the Python reference."""
    return _parked.pop(name, None)


def _check(filename, data, kind):
    """The web app's _read_upload checks, in the same order."""
    if len(data) > MAX_BYTES:
        raise StopAndExplain(
            f"{filename} is larger than the {MAX_BYTES // (1024 * 1024)} MB limit"
        )
    if not data:
        raise StopAndExplain(f"{filename} is empty")
    if kind == "project":
        uploadcheck.check_project(filename, data)
    else:
        uploadcheck.check_slice(filename, data)


def _plan_json(plan):
    return json.dumps(plan_to_dict(plan), indent=2)


def _reorder_report(rep):
    return {
        "json_keys_permuted": list(rep.json_keys_permuted),
        "references_remapped": rep.references_remapped,
        "slices_stripped": list(rep.slices_stripped),
        "sequence_written": rep.sequence_written,
        "sequence_layers": rep.sequence_layers,
        "warnings": list(rep.warnings),
    }


def plan_op(filename, data, slots, plate, first_colour):
    data = bytes(data)
    try:
        _check(filename, data, "project")
        stage1 = plan_from_project(
            data, slots=slots, plate=plate, first_colour=first_colour
        )
    except StopAndExplain as exc:
        return _json({"ok": False, "error": str(exc)})
    plan = stage1.plan
    _park("reordered", bytes(stage1.reordered_project))
    return _json({
        "ok": True,
        "plan": plan_to_dict(plan),
        "plan_json": _plan_json(plan),
        "plan_md": report.plan_markdown(plan),
        "notes": list(stage1.notes),
        "plates": stage1.plates,
        "reorder": _reorder_report(stage1.reorder_report),
        "original_filaments": list(stage1.original_filaments),
        "original_names": list(stage1.original_names),
        "printer_model": stage1.printer_model,
        "plate": stage1.plate,
    })


def _stage3(result, data):
    plan = result.plan
    _park("text", result.edited.text)
    return _json({
        "ok": True,
        "slice_name": result.slice_name,
        "slice_input": (
            "slice_input.3mf" if threemf.looks_like_zip(bytes(data)) else "slice_input.gcode"
        ),
        "verification_ok": result.report.ok,
        "checks": [
            {"name": name, "ok": ok, "detail": detail}
            for (name, ok, detail) in result.report.checks
        ],
        "inserted": result.edited.inserted_blocks,
        "edited_blocks": result.edited.edited_blocks,
        "lines_inserted": result.edited.lines_inserted,
        "notes": [str(note) for note in result.notes],
        "plan": plan_to_dict(plan) if plan is not None else None,
        "plan_json": _plan_json(plan) if plan is not None else None,
        "plan_md": report.plan_markdown(plan) if plan is not None else None,
    })


def convert_op(filename, data, slots, plate):
    data = bytes(data)
    try:
        _check(filename, data, "slice")
        result = plan_and_insert(data, slots=slots, plate=plate)
    except StopAndExplain as exc:
        return _json({"ok": False, "error": str(exc)})
    return _stage3(result, data)


def slice_op(filename, data, plan_json):
    data = bytes(data)
    try:
        plan = plan_from_dict(json.loads(plan_json))
        _check(filename, data, "slice")
        result = insert_into_slice(data, plan)
    except StopAndExplain as exc:
        return _json({"ok": False, "error": str(exc)})
    return _stage3(result, data)


def take_text():
    return _parked.pop("text", "")


def checklist(plan_json, project_name, printer_model):
    plan = plan_from_dict(json.loads(plan_json))
    return report.checklist_markdown(plan, project_name, printer_model)


def repack(data, text, slice_name):
    data = bytes(data)
    try:
        packed = threemf.repack_slice(data, text, slice_name)
    except StopAndExplain as exc:
        return _json({"ok": False, "error": str(exc)})
    _park("output3mf", bytes(packed))
    return _json({"ok": True})


def page(meta_json, plan_json, plan_md, checklist_md, report_json, error):
    """_job_page's template context, as plain JSON."""
    meta = json.loads(meta_json) if meta_json else {}
    plan = plan_from_dict(json.loads(plan_json)) if plan_json else None
    report_data = json.loads(report_json) if report_json else None
    out = {
        "job": meta,
        "plan": None,
        "plan_md": plan_md or "",
        "report": report_data,
        "checklist": checklist_md or "",
        "instructions": [],
        "homes": None,
        "pause_rows": [],
        "error": error or None,
    }
    if plan is not None:
        plan_dict = plan_to_dict(plan)
        out["plan"] = plan_dict
        out["instructions"] = report.stage2_instructions(
            plan, meta.get("original_name", "")
        )
        out["homes"] = {
            str(tool): sorted(positions)
            for tool, positions in report.homes(plan).items()
        }
        # presentation extras live here, so out["plan"] stays plan_to_dict exactly
        out["pause_rows"] = [
            dict(
                row,
                tool_before=plan.tool_before(pause),
                tool_after=plan.tool_after(pause),
                remaining_text=pause.remaining_text,
            )
            for row, pause in zip(report.pause_rows(plan), plan.pauses)
        ]
    return json.dumps(out, ensure_ascii=False)
`;

const PY_NAMES = [
  "take",
  "plan_op",
  "convert_op",
  "slice_op",
  "take_text",
  "checklist",
  "repack",
  "page",
];

/** A user-facing refusal (the app's StopAndExplain sentence). */
class UserError extends Error {}

// None crosses the Pyodide boundary as undefined; a JS null is not the same
// thing to every Pyodide version, so route both through undefined.
function none(value) {
  return value === null || value === undefined ? undefined : value;
}

async function boot() {
  const t0 = now();
  try {
    importScripts(PYODIDE_URL + "pyodide.js");
    pyodide = await loadPyodide({ indexURL: PYODIDE_URL });
  } catch (err) {
    // no CDN, no engine: say so in words the page can show, not a worker trace
    throw new Error(
      "PausePalette could not be downloaded. Check your connection, then reload the page.",
    );
  }
  const engineMs = now() - t0;

  const storageMode = await store.open();

  const tCore = now();
  pyodide.FS.mkdirTree("/pausepalette/dialects");
  const files = [];
  for (const path of CORE_FILES) {
    const res = await fetch(CORE_BASE + path);
    if (!res.ok) throw new Error("Part of the app could not be loaded. Reload the page.");
    const bytes = new Uint8Array(await res.arrayBuffer());
    pyodide.FS.writeFile("/" + path, bytes);
    files.push({ path: path, bytes: bytes.length });
  }
  pyodide.runPython("import sys\nif '/' not in sys.path: sys.path.insert(0, '/')");
  pyodide.runPython(PY_SRC);
  PY = {};
  for (const name of PY_NAMES) PY[name] = pyodide.globals.get(name);
  const coreMs = now() - tCore;

  bootInfo = {
    pyodideVersion: pyodide.version,
    timings: { engineDownloadInitMs: engineMs, coreFetchLoadMs: coreMs },
    core: {
      files: files,
      bytes: files.reduce((n, f) => n + f.bytes, 0),
    },
    resources: resources(),
    storage: storageMode,
  };
  return bootInfo;
}

function ensureBooted() {
  if (!booting) booting = boot();
  return booting;
}

/* --- upload ingestion ---------------------------------------------------- */

// Allocate a Python bytearray of the right size and copy the ArrayBuffer into
// its own storage: no toJs(), no base64, no per-element crossing.
function ingest(arrayBuffer, label) {
  const id = `buf${++bufferSeq}`;
  const proxy = pyodide.runPython(`bytearray(${arrayBuffer.byteLength})`);
  const view = proxy.getBuffer();
  view.data.set(new Uint8Array(arrayBuffer));
  view.release();
  buffers.set(id, { proxy: proxy, bytes: arrayBuffer.byteLength, label: label });
  return id;
}

function bufferProxy(id) {
  const entry = buffers.get(id);
  return entry ? entry.proxy : undefined;
}

function release(id) {
  const entry = buffers.get(id);
  if (!entry) return;
  buffers.delete(id);
  entry.proxy.destroy();
}

// Parked bytes come back as a PyProxy of a Python bytes object: copy them into
// a fresh ArrayBuffer so they can be stored and transferred.
function takeBytes(name) {
  const proxy = PY.take(name);
  if (!proxy) return null;
  const view = proxy.getBuffer();
  const out = new Uint8Array(view.data).buffer;
  view.release();
  proxy.destroy();
  return out;
}

/* --- job identity and download names ------------------------------------- */

function safeName(name) {
  const base = String(name || "").split("/").pop();
  const clean = base.replace(/[^A-Za-z0-9 ._-]+/g, "_").trim().slice(0, 120);
  return clean || "file";
}

function stem(name) {
  const base = String(name || "").split("/").pop();
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(0, dot) : base;
}

function titleFrom(name) {
  return stem(safeName(name)) || "project";
}

function newJobId() {
  // uuid4().hex[:12] in app.py; any 12 hex characters do the same job here
  const bytes = crypto.getRandomValues(new Uint8Array(6));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function intOr(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.trunc(n) : fallback;
}

function mimeFor(name) {
  return MIME[String(name).split(".").pop()] || "application/octet-stream";
}

/* --- storage ------------------------------------------------------------- */

async function save(job) {
  try {
    await store.put(job);
  } catch (err) {
    throw new UserError(
      "This job could not be saved because this browser's storage is full. Delete an old job and try again."
    );
  }
}

async function requireMeta(jobId) {
  const meta = await store.getMeta(String(jobId || ""));
  if (!meta) throw new UserError("This job is no longer in this browser.");
  return meta;
}

async function loadArtifacts(jobId) {
  const out = {};
  for (const name of ["plan.json", "plan.md", "checklist.md", "report.json"]) {
    const rec = await store.getFile(jobId, name);
    out[name] = rec ? rec.value : null;
  }
  return out;
}

async function buildPage(meta, error) {
  const a = await loadArtifacts(meta.id);
  const json = PY.page(
    JSON.stringify(meta),
    none(a["plan.json"]),
    none(a["plan.md"]),
    none(a["checklist.md"]),
    none(a["report.json"]),
    none(error),
  );
  return JSON.parse(json);
}

/* --- stage 1: plan ------------------------------------------------------- */

async function runProjectPlan(meta, filename, file, slots, plate, firstColour) {
  const timings = {};
  const tIngest = now();
  const bid = ingest(file, "project");
  timings.ingestMs = round(now() - tIngest);
  let res;
  try {
    const t0 = now();
    res = JSON.parse(PY.plan_op(filename, bufferProxy(bid), slots, plate, none(firstColour)));
    timings.coreMs = round(now() - t0);
  } finally {
    release(bid);
  }
  if (!res.ok) throw new UserError(res.error);

  const tTake = now();
  const reordered = takeBytes("reordered");
  timings.takeMs = round(now() - tTake);

  // store.reset_meta: keep only the job's identity, replace everything else
  const planned = {
    id: meta.id,
    title: meta.title,
    created: meta.created,
    updated: nowSec(),
    stage: "planned",
    original_name: safeName(filename || "project.3mf"),
    slots: slots,
    plate: res.plate,
    plates: res.plates,
    printer_model: res.printer_model,
    filaments: res.original_filaments,
    filament_names: res.original_names,
    new_order: res.plan.filament_colour,
    pauses: res.plan.pauses.length,
    swaps: res.plan.swaps_total,
    moves: res.plan.colour_moves,
    provisional: res.plan.provisional,
    deferred: res.plan.deferred,
    first_colour: res.plan.first_colour,
    first_colour_choices: res.plan.first_colour_choices,
    reorder_report: res.reorder,
  };
  const files = {
    "project.3mf": { kind: "bytes", value: file },
    "reordered.3mf": { kind: "bytes", value: reordered },
    "plan.json": { kind: "text", value: res.plan_json },
    "plan.md": { kind: "text", value: res.plan_md },
  };
  for (const name of STAGE3_ARTEFACTS) files[name] = null;

  const tStore = now();
  await save({ id: meta.id, meta: planned, files: files });
  timings.storeMs = round(now() - tStore);
  return { meta: planned, timings: timings };
}

async function planJob(msg) {
  const filename = msg.filename || "project.3mf";
  const file = requireFile(msg, filename);
  const slots = intOr(msg.slots, 4);
  const plate = intOr(msg.plate, 1);

  const stamp = nowSec();
  const meta = {
    id: newJobId(),
    title: titleFrom(filename),
    created: stamp,
    updated: stamp,
    stage: "created",
  };
  await save({ id: meta.id, meta: meta });
  try {
    const done = await runProjectPlan(meta, filename, file, slots, plate, msg.firstColour);
    return { ok: true, jobId: meta.id, job: done.meta, page: await buildPage(done.meta), timings: done.timings };
  } catch (err) {
    await store.delete(meta.id).catch(() => {}); // don't leave a half-made job behind
    throw err;
  }
}

/* --- stage 3 / single stage: insert pauses ------------------------------- */

async function finishJob(jobId, res, text, fields, stored, planJson) {
  const timings = {};
  const t0 = now();
  const files = {};
  // _finish_job: the name comes from the incoming meta first, then the stored one
  const base = stem(fields.original_name || stored.original_name || "print").replace(
    /\.gcode$/,
    "",
  );
  files["output.gcode"] = { kind: "text", value: text };

  const sliceInput = await store.getFile(jobId, "slice_input.3mf");
  const has3mf = Boolean(res.slice_name) && Boolean(sliceInput);
  if (has3mf) {
    const bid = ingest(sliceInput.value, "slice_input.3mf");
    try {
      const packed = JSON.parse(PY.repack(bufferProxy(bid), text, res.slice_name));
      if (!packed.ok) throw new UserError(packed.error);
      files["output.3mf"] = { kind: "bytes", value: takeBytes("output3mf") };
    } finally {
      release(bid);
    }
  } else {
    files["output.3mf"] = null;
  }

  // name the downloads after the plate they are for: the plate whose slice was
  // edited when we know it, otherwise the plate the plan was made for
  const sliced = /^Metadata\/plate_(\d+)\.gcode$/.exec(res.slice_name || "");
  const slicePlate = sliced ? Number(sliced[1]) : null;
  const plate = slicePlate || fields.plate || stored.plate;
  const suffix = plate ? `_plate${plate}` : "";

  const plan = planJson ? JSON.parse(planJson) : null;
  const checklist = plan
    ? PY.checklist(
        planJson,
        base,
        none(plan.printer_model || stored.printer_model),
      )
    : "";
  files["checklist.md"] = { kind: "text", value: checklist };
  files["report.json"] = {
    kind: "text",
    value: JSON.stringify({ ok: res.verification_ok, checks: res.checks }, null, 2),
  };

  // store.update_meta: merge into what is already there
  const meta = Object.assign({}, stored, fields, {
    output_name: `${base}_pauses${suffix}.gcode`,
    slice_plate: slicePlate,
    has_output_3mf: has3mf,
    inserted: res.inserted,
    edited_blocks: res.edited_blocks,
    lines_inserted: res.lines_inserted,
    verification_ok: res.verification_ok,
    notes: res.notes,
    updated: nowSec(),
  });
  await save({ id: jobId, meta: meta, files: files });
  timings.finishMs = round(now() - t0);
  return meta;
}

async function runConvert(meta, filename, file, slots, plate) {
  const timings = {};
  const tIngest = now();
  const bid = ingest(file, "slice");
  timings.ingestMs = round(now() - tIngest);
  let res;
  try {
    const t0 = now();
    res = JSON.parse(PY.convert_op(filename, bufferProxy(bid), slots, none(plate)));
    timings.coreMs = round(now() - t0);
  } finally {
    release(bid);
  }
  if (!res.ok) throw new UserError(res.error);

  const tTake = now();
  const text = PY.take_text();
  timings.takeMs = round(now() - tTake);

  const tStore = now();
  await save({
    id: meta.id,
    files: {
      [res.slice_input]: { kind: "bytes", value: file },
      "plan.json": { kind: "text", value: res.plan_json },
      "plan.md": { kind: "text", value: res.plan_md },
    },
  });
  timings.storeMs = round(now() - tStore);

  const fields = {
    stage: "converted",
    original_name: safeName(filename || "slice.gcode"),
    slots: slots,
    plate: res.plan ? res.plan.plate : null,
    printer_model: res.plan ? res.plan.printer_model : null,
    pauses: res.inserted,
    swaps: res.plan ? res.plan.swaps_total : 0,
    moves: res.plan ? res.plan.colour_moves : 0,
  };
  const next = await finishJob(meta.id, res, text, fields, await store.getMeta(meta.id), res.plan_json);
  return { meta: next, timings: timings };
}

async function convertJob(msg) {
  const filename = msg.filename || "slice.gcode";
  const file = requireFile(msg, filename);
  const slots = intOr(msg.slots, 4);

  const stamp = nowSec();
  const meta = {
    id: newJobId(),
    title: titleFrom(filename),
    created: stamp,
    updated: stamp,
    stage: "created",
  };
  await save({ id: meta.id, meta: meta });
  try {
    const done = await runConvert(meta, filename, file, slots, null);
    return { ok: true, jobId: meta.id, job: done.meta, page: await buildPage(done.meta), timings: done.timings };
  } catch (err) {
    await store.delete(meta.id).catch(() => {});
    throw err;
  }
}

async function sliceJob(msg) {
  const jobId = String(msg.jobId || "");
  const filename = msg.filename || "slice";
  const file = requireFile(msg, filename);
  const stored = await requireMeta(jobId);
  const planFile = await store.getFile(jobId, "plan.json");
  if (!planFile) throw new UserError("This job is no longer in this browser.");

  const timings = {};
  const tIngest = now();
  const bid = ingest(file, "slice");
  timings.ingestMs = round(now() - tIngest);
  let res;
  try {
    const t0 = now();
    res = JSON.parse(PY.slice_op(filename, bufferProxy(bid), planFile.value));
    timings.coreMs = round(now() - t0);
  } finally {
    release(bid);
  }
  // A refused slice changes nothing: app.py writes no artefact before this point.
  if (!res.ok) throw new UserError(res.error);

  const tTake = now();
  const text = PY.take_text();
  timings.takeMs = round(now() - tTake);

  const files = {
    [res.slice_input]: { kind: "bytes", value: file },
  };
  let planJson = planFile.value;
  const fields = { stage: "applied", slice_name: safeName(filename || "slice") };
  if (res.plan) {
    // the slice finalises a provisional/deferred plan: keep the resolved one
    planJson = res.plan_json;
    files["plan.json"] = { kind: "text", value: res.plan_json };
    files["plan.md"] = { kind: "text", value: res.plan_md };
    fields.pauses = res.plan.pauses.length;
    fields.swaps = res.plan.swaps_total;
    fields.provisional = res.plan.provisional;
    fields.deferred = res.plan.deferred;
  }
  const tStore = now();
  await save({ id: jobId, files: files });
  timings.storeMs = round(now() - tStore);

  const meta = await finishJob(jobId, res, text, fields, stored, planJson);
  return { ok: true, jobId: jobId, job: meta, page: await buildPage(meta), timings: timings };
}

/* --- re-running and removing a job -------------------------------------- */

async function regenerateJob(msg) {
  const jobId = String(msg.jobId || "");
  const meta = await requireMeta(jobId);
  const slots = intOr(meta.slots, 4);
  const name = meta.original_name || "upload";

  const project = await store.getFile(jobId, "project.3mf");
  const slice3mf = project === null ? await store.getFile(jobId, "slice_input.3mf") : null;
  const sliceGcode =
    project === null && slice3mf === null ? await store.getFile(jobId, "slice_input.gcode") : null;

  let done;
  if (project) {
    done = await runProjectPlan(meta, name, project.value, slots, meta.plate || 1, none(meta.first_colour));
  } else if (slice3mf) {
    done = await runConvert(meta, name, slice3mf.value, slots, none(meta.plate));
  } else if (sliceGcode) {
    done = await runConvert(meta, name, sliceGcode.value, slots, none(meta.plate));
  } else {
    throw new UserError("This job no longer has its original file, so it cannot be regenerated.");
  }
  return { ok: true, jobId: jobId, job: done.meta, page: await buildPage(done.meta), timings: done.timings };
}

async function firstColourJob(msg) {
  const jobId = String(msg.jobId || "");
  const meta = await requireMeta(jobId);
  const colour = intOr(msg.colour, 0);
  const project = await store.getFile(jobId, "project.3mf");
  if (!project) throw new UserError("This job no longer has its original project, so it cannot be regenerated.");
  const choices = meta.first_colour_choices || [];
  if (choices.length && !choices.includes(colour)) {
    throw new UserError("that colour is not one of the first layer's colours");
  }
  const done = await runProjectPlan(
    meta,
    meta.original_name || "project.3mf",
    project.value,
    intOr(meta.slots, 4),
    meta.plate || 1,
    colour,
  );
  return { ok: true, jobId: jobId, job: done.meta, page: await buildPage(done.meta), timings: done.timings };
}

/* --- jobs and downloads -------------------------------------------------- */

async function listJobs() {
  const jobs = await store.list();
  jobs.sort((a, b) => (b.updated || 0) - (a.updated || 0));
  return { ok: true, jobs: jobs, storage: store.mode };
}

async function getJob(msg) {
  const jobId = String(msg.jobId || "");
  const meta = await requireMeta(jobId);
  const files = (await store.files(jobId)).map((rec) => ({
    name: rec.name,
    kind: rec.kind,
    bytes: store.size(rec),
  }));
  files.sort((a, b) => a.name.localeCompare(b.name));
  return { ok: true, jobId: jobId, job: meta, files: files, page: await buildPage(meta) };
}

async function deleteJob(msg) {
  const jobId = String(msg.jobId || "");
  await store.delete(jobId);
  return { ok: true, jobId: jobId };
}

async function clearJobs() {
  await store.clear();
  return { ok: true };
}

async function readBuffer(msg) {
  const jobId = String(msg.jobId || "");
  const name = String(msg.name || "");
  if (!DOWNLOAD_WHITELIST.has(name)) throw new UserError("That file is no longer available in this job.");
  const rec = await store.getFile(jobId, name);
  if (!rec) throw new UserError("That file is no longer available in this job.");
  const meta = (await store.getMeta(jobId)) || {};

  // the file names a finished job hands the user
  const base = stem(meta.original_name || "print").replace(/\.gcode$/, "");
  const plate = meta.plate;
  const outputName = meta.output_name || `${base}_pauses.gcode`;
  const names = {
    "reordered.3mf": `${base}_pausepalette${plate ? `_plate${plate}` : ""}.3mf`,
    "output.gcode": outputName,
    "output.3mf": `${outputName}.3mf`,
  };
  const downloadName = names[name] || name;

  let buffer;
  if (rec.kind === "text") buffer = new TextEncoder().encode(rec.value).buffer;
  else if (rec.value instanceof Blob) buffer = await rec.value.arrayBuffer();
  else buffer = rec.value; // IndexedDB already handed us a private copy

  return {
    ok: true,
    jobId: jobId,
    name: name,
    downloadName: downloadName,
    mime: mimeFor(downloadName),
    bytes: buffer.byteLength,
    buffer: buffer,
    transfer: [buffer],
  };
}

/* --- message pump -------------------------------------------------------- */

function requireFile(msg, filename) {
  if (!(msg.file instanceof ArrayBuffer)) {
    throw new UserError(`${filename} was not received — try the upload again`);
  }
  return msg.file;
}

async function handle(msg) {
  switch (msg.type) {
    case "init": {
      const info = await ensureBooted();
      return { ok: true, ...info };
    }
    case "plan":
      await ensureBooted();
      return planJob(msg);
    case "convert":
      await ensureBooted();
      return convertJob(msg);
    case "slice":
      await ensureBooted();
      return sliceJob(msg);
    case "regenerate":
      await ensureBooted();
      return regenerateJob(msg);
    case "firstColour":
      await ensureBooted();
      return firstColourJob(msg);
    case "readBuffer":
      await ensureBooted();
      return readBuffer(msg);
    case "listJobs":
      await ensureBooted();
      return listJobs();
    case "getJob":
      await ensureBooted();
      return getJob(msg);
    case "deleteJob":
      await ensureBooted();
      return deleteJob(msg);
    case "clearJobs":
      await ensureBooted();
      return clearJobs();
    default:
      throw new Error(`unknown message type ${msg.type}`);
  }
}

self.onmessage = async (event) => {
  const msg = event.data || {};
  const id = msg.id;
  const started = now();
  let reply;
  try {
    reply = await handle(msg);
  } catch (err) {
    const user = err instanceof UserError;
    reply = {
      ok: false,
      error: String((err && err.message) || err),
      unexpected: !user,
    };
    // app.py answered a refused slice / first colour / regenerate with the job
    // page plus an error banner; do the same so the UI keeps the job on screen.
    if (user && msg.jobId && ["slice", "firstColour", "regenerate"].includes(msg.type)) {
      try {
        const meta = await store.getMeta(String(msg.jobId));
        if (meta) {
          reply.jobId = meta.id;
          reply.job = meta;
          reply.page = await buildPage(meta, reply.error);
        }
      } catch (_) {
        // the job is gone too: the plain error is the whole answer
      }
    }
  }
  const transfer = reply.transfer || [];
  delete reply.transfer;
  const timings = Object.assign({}, reply.timings, { totalMs: round(now() - started) });
  self.postMessage({ id: id, ...reply, timings: timings, diag: diag() }, transfer);
};
