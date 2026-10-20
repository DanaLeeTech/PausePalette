/* Deterministic test of the worker protocol, the job store and the hashes.
 *
 * Run in two page loads: the first plans a project and reloads; the second
 * proves the job was still there after the reload, then exercises convert,
 * first colour, refused slices, delete and clear. window.__selftest holds the
 * result and #results shows the same JSON.
 */
"use strict";

const PHASE_KEY = "pp.selftest.phase";
const JOB_KEY = "pp.selftest.jobId";
const RESULT_KEY = "pp.selftest.results";

const results = {
  selftest: "pausepalette-protocol-v2",
  status: "RUNNING",
  phase: "booting",
  checks: {},
  values: {},
  timingsMs: {},
  errors: [],
};

let worker = null;

function show() {
  document.getElementById("results").textContent = JSON.stringify(results, null, 2);
  document.getElementById("phase").textContent = `phase: ${results.phase}`;
  window.__selftest = results;
}

function note(text) {
  document.getElementById("status").textContent = text;
  show();
}

function check(name, ok, detail) {
  results.checks[name] = ok ? true : false;
  if (detail !== undefined) results.values[name] = detail;
  show();
}

function allPassed() {
  return Object.values(results.checks).every((v) => v === true) && results.errors.length === 0;
}

function finish(ok) {
  results.pass = ok && allPassed();
  results.status = results.pass ? "PASS" : "FAIL";
  document.getElementById("status").textContent = results.status;
  show();
  if (worker) worker.terminate();
}

/* --- RPC ----------------------------------------------------------------- */

function runReply(send, message, transfer) {
  return new Promise((resolve, reject) => {
    const id = Math.random().toString(36).slice(2);
    const onMessage = (event) => {
      if (!event.data || event.data.id !== id) return;
      send.removeEventListener("message", onMessage);
      resolve(event.data);
    };
    send.addEventListener("message", onMessage);
    send.postMessage({ ...message, id }, transfer || []);
    setTimeout(() => {
      send.removeEventListener("message", onMessage);
      reject(new Error(`timeout waiting for ${message.type}`));
    }, 300000);
  });
}

async function run(message, transfer) {
  const reply = await runReply(worker, message, transfer);
  if (!reply.ok) throw new Error(reply.error);
  return reply;
}

/* --- helpers ------------------------------------------------------------- */

function sha256Hex(bytes) {
  return crypto.subtle.digest("SHA-256", bytes).then((digest) =>
    [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join(""),
  );
}

// Python None arrives as undefined through JSON in some paths; normalise so a
// comparison with the CLI's plan cannot fail on that alone.
function canon(value) {
  if (value === undefined) return null;
  if (Array.isArray(value)) return value.map(canon);
  if (value && typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value).sort()) out[key] = canon(value[key]);
    return out;
  }
  return value;
}

function canonicalJson(value) {
  return JSON.stringify(canon(value));
}

function ms(...pairs) {
  const out = {};
  for (const [name, value] of pairs) if (typeof value === "number") out[name] = Math.round(value);
  return out;
}

async function fetchBytes(url) {
  // the built tree keeps samples/ beside the page; web/ itself has them one up
  for (const candidate of [url, "../" + url]) {
    const res = await fetch(candidate);
    if (res.ok) return res.arrayBuffer();
  }
  throw new Error(`fetch ${url}: not found`);
}

function heap(step, reply) {
  const list = results.values.bufferHeap || (results.values.bufferHeap = []);
  list.push({ step: step, count: reply.diag.buffers.count, bytes: reply.diag.buffers.bytes });
  return reply.diag.buffers.count;
}

// The checks made before the reload would be lost with the page, so carry them
// over and report one merged result.
function restore() {
  const saved = sessionStorage.getItem(RESULT_KEY);
  sessionStorage.removeItem(RESULT_KEY);
  if (!saved) return;
  const prev = JSON.parse(saved);
  const heaps = [...(prev.values.bufferHeap || []), ...(results.values.bufferHeap || [])];
  results.checks = { ...prev.checks, ...results.checks };
  results.values = { ...prev.values, ...results.values };
  if (heaps.length) results.values.bufferHeap = heaps;
  results.timingsMs = { ...prev.timingsMs, ...results.timingsMs };
  results.pyodideVersion = prev.pyodideVersion;
  show();
}

/* --- phase 1: plan, hash, reload ---------------------------------------- */

async function phaseOne() {
  results.phase = "before-reload";
  const init = await run({ type: "init" });
  results.pyodideVersion = init.pyodideVersion;
  results.timingsMs.init = Math.round(
    init.timings.engineDownloadInitMs + init.timings.coreFetchLoadMs,
  );
  check("init", init.ok === true && init.core.files.length === 17, {
    coreFiles: init.core.files.length,
    coreBytes: init.core.bytes,
    pyodideVersion: init.pyodideVersion,
  });
  check("storage_indexeddb", init.storage === "indexeddb", init.storage);

  const expected = await (await fetch("expected.json")).json();

  const project = await fetchBytes("samples/Keychain.3mf");
  results.values.projectBytes = project.byteLength;
  const planned = await run(
    { type: "plan", filename: "Keychain.3mf", slots: 4, plate: 1, file: project },
    [project],
  );
  results.timingsMs.plan = planned.timings;
  results.values.planJobId = planned.jobId;
  check("plan_ok", planned.ok === true && planned.job.stage === "planned", {
    jobId: planned.jobId,
    stage: planned.job.stage,
    pauses: planned.job.pauses,
    swaps: planned.job.swaps,
    title: planned.job.title,
  });

  const page = planned.page;
  check("plan_page_has_plan", Boolean(page && page.plan), {
    instructions: page ? page.instructions.length : null,
    homes: page ? Object.keys(page.homes || {}).length : null,
    pauseRows: page ? page.pause_rows.length : null,
  });

  const planJson = canonicalJson(page.plan);
  const expectedJson = canonicalJson(expected.plan);
  check("plan_json_matches_cli", planJson === expectedJson, {
    planChars: planJson.length,
    cliChars: expectedJson.length,
    planSha: await sha256Hex(new TextEncoder().encode(planJson)),
    cliSha: await sha256Hex(new TextEncoder().encode(expectedJson)),
  });

  const reordered = await run({ type: "readBuffer", jobId: planned.jobId, name: "reordered.3mf" });
  const reorderedSha = await sha256Hex(new Uint8Array(reordered.buffer));
  results.timingsMs.readBuffer = reordered.timings;
  check("reordered_3mf_sha256_matches_cli", reorderedSha === expected.reordered_3mf_sha256, {
    got: reorderedSha,
    cli: expected.reordered_3mf_sha256,
    bytes: reordered.bytes,
    downloadName: reordered.downloadName,
  });

  const storedPlan = await run({ type: "readBuffer", jobId: planned.jobId, name: "plan.json" });
  const storedText = new TextDecoder().decode(storedPlan.buffer);
  check("stored_plan_json_matches_cli", canonicalJson(JSON.parse(storedText)) === expectedJson, {
    bytes: storedPlan.bytes,
  });

  const planMd = await run({ type: "readBuffer", jobId: planned.jobId, name: "plan.md" });
  check("plan_markdown_written", planMd.bytes > 0, planMd.downloadName);

  results.values.bufferCountAfterPlan = heap("after plan/readBuffers", reordered);
  check("buffers_freed_after_plan", results.values.bufferCountAfterPlan === 0);

  localStorage.setItem(JOB_KEY, planned.jobId);
  sessionStorage.setItem(PHASE_KEY, "after");
  sessionStorage.setItem(RESULT_KEY, JSON.stringify(results));
  results.phase = "reloading";
  note("reloading to prove the job persisted");
  worker.terminate();
  location.reload();
}

/* --- phase 2: after the reload ------------------------------------------ */

async function phaseTwo() {
  results.phase = "after-reload";
  const init = await run({ type: "init" });
  check("init_after_reload", init.ok === true && init.storage === "indexeddb", init.storage);

  const expected = await (await fetch("expected.json")).json();
  const jobId = localStorage.getItem(JOB_KEY);

  const listed = await run({ type: "listJobs" });
  check("job_in_indexeddb_after_reload", listed.jobs.some((job) => job.id === jobId), {
    jobId: jobId,
    jobs: listed.jobs.map((job) => job.id),
    storage: listed.storage,
  });

  const got = await run({ type: "getJob", jobId });
  results.timingsMs.getJob = got.timings;
  const persistedPlan = canonicalJson(got.page.plan);
  check("plan_survives_reload", persistedPlan === canonicalJson(expected.plan), {
    stage: got.job.stage,
    pauses: got.page.plan ? got.page.plan.pauses.length : null,
    files: got.files.map((f) => `${f.name}:${f.bytes}`),
    planSha: await sha256Hex(new TextEncoder().encode(persistedPlan)),
  });
  check("reordered_file_stored", got.files.some((f) => f.name === "reordered.3mf" && f.kind === "bytes"));

  // convert: the single-stage path on an already sliced file
  const gcode = await fetchBytes("samples/Keychain.gcode");
  results.values.gcodeBytes = gcode.byteLength;
  const converted = await run(
    { type: "convert", filename: "Keychain.gcode", slots: 4, file: gcode },
    [gcode],
  );
  results.timingsMs.convert = converted.timings;
  results.values.convertJobId = converted.jobId;
  check("convert_ok", converted.ok === true && converted.job.stage === "converted", {
    jobId: converted.jobId,
    pauses: converted.job.pauses,
    outputName: converted.job.output_name,
    verification_ok: converted.job.verification_ok,
  });
  check("convert_verified", converted.job.verification_ok === true && converted.page.report.ok === true, {
    checks: converted.page.report.checks.length,
    failures: converted.page.report.checks.filter((c) => !c.ok).map((c) => c.name),
  });

  const output = await run({ type: "readBuffer", jobId: converted.jobId, name: "output.gcode" });
  const outputSha = await sha256Hex(new Uint8Array(output.buffer));
  check("convert_output_sha256_matches_cli", outputSha === expected.edited_gcode_sha256, {
    got: outputSha,
    cli: expected.edited_gcode_sha256,
    bytes: output.bytes,
    downloadName: output.downloadName,
    mime: output.mime,
  });
  results.values.bufferCountAfterConvert = heap("after convert/readBuffer", output);
  check("buffers_freed_after_convert", results.values.bufferCountAfterConvert === 0);

  const checklist = await run({ type: "readBuffer", jobId: converted.jobId, name: "checklist.md" });
  const checklistText = new TextDecoder().decode(checklist.buffer);
  check("checklist_written", checklistText.includes("Print from file") && checklist.bytes > 0, {
    bytes: checklist.bytes,
  });

  // a sliced project goes back out as a sliced project: output.3mf is the same
  // archive with the edited G-code (and its checksum) put back in
  const project3mf = await fetchBytes("samples/Keychain.3mf");
  const converted3mf = await run(
    { type: "convert", filename: "Keychain.3mf", slots: 4, file: project3mf },
    [project3mf],
  );
  const out3mf = await run({ type: "readBuffer", jobId: converted3mf.jobId, name: "output.3mf" });
  const magic = new Uint8Array(out3mf.buffer, 0, 2);
  const out3mfGcode = await run({ type: "readBuffer", jobId: converted3mf.jobId, name: "output.gcode" });
  check(
    "convert_3mf_repacked",
    converted3mf.job.has_output_3mf === true &&
      out3mf.buffer.byteLength > 1000 &&
      magic[0] === 0x50 &&
      magic[1] === 0x4b,
    {
      hasOutput3mf: converted3mf.job.has_output_3mf,
      output3mfBytes: out3mf.bytes,
      outputName: converted3mf.job.output_name,
      downloadName: out3mf.downloadName,
      gcodeSha: await sha256Hex(new Uint8Array(out3mfGcode.buffer)),
      expectedGcodeSha: expected.edited_gcode_sha256,
    },
  );
  results.values.bufferCountAfterConvert3mf = heap("after convert(.3mf)/readBuffers", out3mf);
  check("buffers_freed_after_convert_3mf", results.values.bufferCountAfterConvert3mf === 0);

  // first colour: re-plan the stored project from a chosen colour
  const before = await run({ type: "getJob", jobId });
  const beforeJson = canonicalJson(before.page.plan);
  const colours = before.page.plan.first_colour_choices.length
    ? before.page.plan.first_colour_choices
    : before.page.plan.original_filaments.map((_, index) => index + 1);
  const firstColour = await run({ type: "firstColour", jobId, colour: colours[0] });
  results.timingsMs.firstColour = firstColour.timings;
  const afterJson = canonicalJson(firstColour.page.plan);
  check("first_colour_ok", firstColour.ok === true && firstColour.job.stage === "planned", {
    colour: colours[0],
    colours: colours,
    pauses: firstColour.page.plan.pauses.length,
    planChanged: beforeJson !== afterJson,
  });
  check(
    "first_colour_plan_valid",
    Array.isArray(firstColour.page.plan.pauses) &&
      typeof firstColour.page.plan.slots === "number" &&
      Array.isArray(firstColour.page.plan.filament_colour),
    { slots: firstColour.page.plan.slots, filamentColour: firstColour.page.plan.filament_colour },
  );
  const replanned = await run({ type: "readBuffer", jobId, name: "reordered.3mf" });
  check(
    "first_colour_reordered_still_matches_cli",
    (await sha256Hex(new Uint8Array(replanned.buffer))) === expected.reordered_3mf_sha256,
  );
  results.values.bufferCountAfterFirstColour = heap("after firstColour", replanned);
  check("buffers_freed_after_first_colour", results.values.bufferCountAfterFirstColour === 0);

  // regenerate re-runs the stored input, exactly as a re-upload would: once for
  // a project (stage 1) and once for a converted slice (stage 3)
  const regenPlan = await run({ type: "regenerate", jobId: jobId });
  const regenPlanFile = await run({ type: "readBuffer", jobId, name: "reordered.3mf" });
  check(
    "regenerate_project_ok",
    regenPlan.ok === true &&
      regenPlan.job.stage === "planned" &&
      (await sha256Hex(new Uint8Array(regenPlanFile.buffer))) === expected.reordered_3mf_sha256,
    { stage: regenPlan.job.stage, pauses: regenPlan.job.pauses },
  );
  const regenSlice = await run({ type: "regenerate", jobId: converted.jobId });
  const regenSliceOut = await run({ type: "readBuffer", jobId: converted.jobId, name: "output.gcode" });
  check(
    "regenerate_slice_ok",
    regenSlice.ok === true &&
      regenSlice.job.stage === "converted" &&
      (await sha256Hex(new Uint8Array(regenSliceOut.buffer))) === expected.edited_gcode_sha256,
    { stage: regenSlice.job.stage, pauses: regenSlice.job.pauses },
  );
  results.values.bufferCountAfterRegenerate = heap("after regenerate", regenSliceOut);
  check("buffers_freed_after_regenerate", results.values.bufferCountAfterRegenerate === 0);

  // a slice that does not match the plan is refused, and the job is untouched
  const planHashBefore = await sha256Hex(new TextEncoder().encode(beforeJson));
  const mismatched = await fetchBytes("samples/Keychain.gcode");
  const refused = await runReply(worker, {
    type: "slice",
    jobId: jobId,
    filename: "Keychain.gcode",
    file: mismatched,
  }, [mismatched]);
  check(
    "slice_mismatch_refused",
    refused.ok === false && /does not match the plan|differs from the plan/.test(refused.error || ""),
    { error: refused.error, unexpected: refused.unexpected },
  );
  check("slice_mismatch_page_keeps_job", refused.jobId === jobId && refused.page && refused.page.error === refused.error, {
    pageError: refused.page ? refused.page.error : null,
  });
  results.values.bufferCountAfterRefusedSlice = heap("after refused slice", refused);
  check("buffers_freed_after_refused_slice", results.values.bufferCountAfterRefusedSlice === 0);

  const afterRefusal = await run({ type: "getJob", jobId });
  check(
    "refused_slice_keeps_plan",
    (await sha256Hex(new TextEncoder().encode(canonicalJson(afterRefusal.page.plan)))) === planHashBefore,
    { stage: afterRefusal.job.stage, files: afterRefusal.files.map((f) => f.name) },
  );
  check(
    "refused_slice_wrote_nothing",
    afterRefusal.job.stage === "planned" &&
      !afterRefusal.files.some((f) => f.name === "output.gcode" || f.name === "slice_input.gcode"),
  );

  // garbage that is not G-code at all: the upload gate refuses it
  const junk = new TextEncoder().encode("this is not gcode\n".repeat(40)).buffer;
  const junkReply = await runReply(worker, {
    type: "slice",
    jobId: jobId,
    filename: "notes.gcode",
    file: junk,
  }, [junk]);
  check(
    "slice_garbage_refused",
    junkReply.ok === false && /filament_colour/.test(junkReply.error || ""),
    { error: junkReply.error },
  );

  // delete and clear
  const deleted = await run({ type: "deleteJob", jobId });
  results.timingsMs.deleteJob = deleted.timings;
  const gone = await runReply(worker, { type: "getJob", jobId });
  check("delete_removes_job", deleted.ok === true && gone.ok === false && /no such job/.test(gone.error || ""), {
    getError: gone.error,
    buffersAfterDelete: deleted.diag.buffers,
  });
  results.values.bufferCountAfterDelete = heap("after delete", deleted);
  check("buffers_freed_after_delete", results.values.bufferCountAfterDelete === 0 && deleted.diag.buffers.bytes === 0);

  const leftovers = await run({ type: "listJobs" });
  check("delete_gone_from_list", !leftovers.jobs.some((job) => job.id === jobId), {
    jobs: leftovers.jobs.map((job) => job.id),
  });

  const cleared = await run({ type: "clearJobs" });
  const empty = await run({ type: "listJobs" });
  check("clear_empties_store", cleared.ok === true && empty.jobs.length === 0, {
    jobs: empty.jobs.length,
  });
}

/* --- run ---------------------------------------------------------------- */

async function main() {
  worker = new Worker("worker.js");
  worker.addEventListener("error", (event) => {
    results.errors.push(`worker error: ${event.message || event.type}`);
    finish(false);
  });
  show();

  try {
    if (sessionStorage.getItem(PHASE_KEY) === "after") {
      restore();
      await phaseTwo();
      sessionStorage.removeItem(PHASE_KEY);
      finish(true);
    } else {
      await phaseOne(); // reloads the page; nothing runs after it
    }
  } catch (err) {
    results.errors.push(String((err && err.stack) || err));
    finish(false);
  }
}

main();
