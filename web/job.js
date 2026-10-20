/* One job: plan, stage-2 instructions, slice upload, verification, checklist.
 * The pieces it shares with the index come from render.js. */
"use strict";

const engine = PP.rpc();
const jobId = new URLSearchParams(location.search).get("job");
const content = document.getElementById("content");
const errorBox = document.getElementById("error");
const boot = document.getElementById("boot");
const ui = PP.ui;

function showError(message) {
  errorBox.innerHTML = message
    ? `<div class="banner error"><strong>Stopped:</strong> ${PP.esc(message)}</div>`
    : "";
}

function busy(button, working, label) {
  if (working) {
    button.dataset.label = button.innerHTML;
    if (label !== undefined) button.textContent = label;
  } else if (button.dataset.label !== undefined) {
    button.innerHTML = button.dataset.label;
  }
  button.disabled = working;
}

function has(value) {
  return value !== null && value !== undefined;
}

/* --- the page ------------------------------------------------------------ */

function statsRow(job, plan) {
  const value = (item) => (has(item) ? PP.esc(item) : "—");
  const stats = [
    ["Stage", value(job.stage)],
    ["Printer", value(job.printer_model)],
    ["Slots", value(job.slots)],
    ["Plate", value(job.plate)],
    ["Pauses", value(job.pauses)],
    ["Swaps", value(job.swaps)],
    ["Position moves", value(job.moves)],
    ["Plan", job.deferred ? "deferred" : job.provisional ? "provisional" : "exact"],
  ];
  const colours = plan && plan.filament_colour;
  if (colours && colours.length) {
    stats.push([
      "Colours",
      `<span class="stats-chips">${colours.map((_, index) => ui.chip(plan, index, "sm")).join("")}</span>`,
    ]);
  }
  return stats
    .map(
      ([key, shown]) =>
        `<div><span class="k">${PP.esc(key)}</span><span class="v">${shown}</span></div>`,
    )
    .join("");
}

function headCard(job, page) {
  const plan = page.plan;
  const plates = job.plates || [];
  const plateNote =
    plates.length > 1
      ? `<p class="muted">Plates in this project:
          ${plates
            .map(
              (plate) =>
                `${PP.esc(plate.id)}${plate.name ? ` (${PP.esc(plate.name)})` : ""}${plate.has_slice ? " sliced" : ""}`,
            )
            .join(", ")}.
        </p>`
      : "";
  return `<section class="card">
    <div class="job-head">
      <h1>${PP.esc(job.title)}</h1>
      ${ui.jobActions(job)}
    </div>
    <div class="stats">${statsRow(job, plan)}</div>
    ${plateNote}
  </section>`;
}

function stageTwoCard(job, page) {
  if (job.stage !== "planned") return "";
  const plan = page.plan;
  const instructions = (page.instructions || [])
    .map((step) => `<li>${PP.esc(step)}</li>`)
    .join("");
  const filament =
    plan && has(plan.filament_name) && plan.filament_name.length
      ? `<h3 class="sub">Filament list, in order</h3>${ui.filamentOrder(plan)}`
      : "";
  const choices = (plan && plan.first_colour_choices) || [];
  const picker =
    choices.length > 1
      ? `<form class="first-colour" id="firstColourForm">
          <label>First colour printed
            <select name="first_colour">
              ${choices
                .map((colour) => {
                  const names = plan.original_names || [];
                  const label = colour - 1 < names.length ? names[colour - 1] : `T${colour - 1}`;
                  return `<option value="${colour}"${colour === plan.first_colour ? " selected" : ""}>${PP.esc(label)}</option>`;
                })
                .join("")}
            </select>
          </label>
          <button class="btn ghost" type="submit">Regenerate plan</button>
          <span class="muted">the plan is worked out again from that colour</span>
        </form>`
      : "";
  const report = job.reorder_report || {};
  const details = job.reorder_report
    ? `<details>
        <summary>What the reorder changed</summary>
        <ul class="muted">
          <li>filament arrays permuted: ${PP.esc((report.json_keys_permuted || []).length)}</li>
          <li>filament references remapped: ${PP.esc(report.references_remapped)}</li>
          <li>stale slice entries stripped: ${PP.esc((report.slices_stripped || []).join(", ") || "none")}</li>
          ${(report.warnings || []).map((warning) => `<li>warning: ${PP.esc(warning)}</li>`).join("")}
        </ul>
      </details>`
    : "";
  return `<section class="card">
    <h2><span class="step">2</span> In Bambu Studio</h2>
    <ol class="steps">${instructions}</ol>
    ${filament}
    ${picker}
    <div class="downloads">
      <button class="btn" type="button" data-download="reordered.3mf">Download reordered project</button>
      <button class="btn ghost" type="button" data-download="plan.md">Plan (markdown)</button>
      <button class="btn ghost" type="button" data-download="plan.json">Plan (json)</button>
      <button class="btn ghost" type="button" data-download="project.3mf">Original project</button>
    </div>
    ${details}
  </section>
  <section class="card">
    <h2><span class="step">3</span> Upload the slice back</h2>
    <p class="muted">After slicing and exporting the plate sliced file, upload it here.
       PausePalette checks it against the plan before touching it.</p>
    <form id="sliceForm">
      <div class="field">
        <span class="field-label">Sliced file (.gcode or .3mf)</span>
        <div class="file-row">
          <button class="btn ghost" type="button" data-pick>Choose file</button>
          <span class="muted file-name">no file chosen</span>
        </div>
        <input class="sr" type="file" accept=".gcode,.3mf,text/plain,application/zip" tabindex="-1" aria-hidden="true">
      </div>
      <button type="submit">Verify &amp; insert pauses</button>
    </form>
  </section>`;
}

function planCard(job, page) {
  const plan = page.plan;
  if (!plan) return "";
  const rows = page.pause_rows || [];
  const pauses = plan.pauses || [];
  let body;
  if (plan.deferred) {
    body = `<div class="banner">
      <strong>This project was not sliced</strong>, so the pause points are not known yet.
      The filament order above is what to use; the exact pause schedule is computed
      automatically when you upload the slice in the next step.
    </div>`;
  } else {
    body = `<h3 class="sub">Load the AMS</h3>${ui.slotsRow(plan, plan.initial, plan.slots)}`;
    if (plan.provisional) {
      body += (plan.expected_demands || []).length
        ? `<p class="muted">Provisional: taken from the full sequence Bambu Studio stored in the
           project. The exact pause positions are resolved from your slice in step 3.</p>`
        : `<p class="muted">Provisional: worked out from the project before slicing — which filament each
           layer prints with, so later layers are planned too. The exact pause count and positions
           are confirmed from your slice in step 3.</p>`;
    }
    if ((plan.timeline || []).length > 1) {
      body += `<h3 class="sub">How the slots evolve</h3>${ui.timeline(plan, rows)}`;
    }
    if (pauses.length) {
      body += `<h3 class="sub">Pauses</h3><div class="pauses">${pauses
        .map((pause, index) => ui.pauseCard(plan, pause, rows[index], pauses.length))
        .join("")}</div>`;
    } else if (plan.provisional && !(plan.expected_demands || []).length) {
      body += `<p class="ok">The colours used fit in the AMS — the full schedule is confirmed
         from your slice.</p>`;
    } else {
      body += `<p class="ok">All colours fit in the AMS — no pauses are needed.</p>`;
    }
    if (page.homes) {
      const tools = Object.keys(page.homes).sort((a, b) => Number(a) - Number(b));
      body += `<h3 class="sub">Positions per colour</h3><ul class="homes">${tools
        .map(
          (tool) => `<li>
            ${ui.colour(plan, Number(tool))}
            <span class="homes-pos">${PP.esc(
              (page.homes[tool] || []).slice().sort((a, b) => a - b).join(", "),
            )}</span>
          </li>`,
        )
        .join("")}</ul>`;
    }
  }
  const warnings = (plan.warnings || []).map((warning) => `<li>${PP.esc(warning)}</li>`).join("");
  const note = pauses.length
    ? ` <span class="h2-note">${pauses.length} pauses · ${PP.esc(plan.swaps_total)} swaps</span>`
    : "";
  return `<section class="card">
    <h2>Colour plan${note}</h2>
    ${body}
    ${warnings ? `<ul class="muted">${warnings}</ul>` : ""}
  </section>`;
}

function reportCards(job, page) {
  const report = page.report;
  if (!report) return "";
  const plan = page.plan;
  const rows = page.pause_rows || [];
  const pauses = (plan && plan.pauses) || [];

  const checks = (report.checks || [])
    .map(
      (check) => `<tr>
        <td>${PP.esc(check.name)}</td>
        <td>${check.ok ? "ok" : "FAIL"}</td>
        <td class="muted">${PP.esc(check.detail)}</td>
      </tr>`,
    )
    .join("");
  const verification = `<section class="card">
    <h2>Verification</h2>
    <p class="${report.ok ? "ok" : "bad"}">${
      report.ok ? "All checks passed." : "Checks failed — do not print this file."
    }</p>
    <div class="table-scroll">
    <table>
      <thead><tr><th>Check</th><th>Result</th><th>Detail</th></tr></thead>
      <tbody>${checks}</tbody>
    </table>
    </div>
  </section>`;

  const outputName = PP.esc(job.output_name);
  const checklist = pauses.length
    ? `<h3 class="sub">Before you start — load the AMS</h3>
       ${ui.slotsRow(plan, plan.initial, plan.slots)}
       <h3 class="sub">Swap checklist <span class="h2-note">tick them off as you go</span></h3>
       <div class="pauses">${pauses
         .map((pause, index) => ui.pauseCard(plan, pause, rows[index], pauses.length, true))
         .join("")}</div>`
    : `<details open><summary>Swap checklist</summary><pre class="checklist">${PP.esc(page.checklist)}</pre></details>`;

  const output = `<section class="card">
    <h2>Output</h2>
    <div class="downloads">
      ${
        job.has_output_3mf
          ? `<button class="btn" type="button" data-download="output.3mf">Download ${outputName}.3mf</button>`
          : ""
      }
      <button class="btn${job.has_output_3mf ? " ghost" : ""}" type="button" data-download="output.gcode">Download ${outputName}</button>
      <button class="btn ghost" type="button" data-download="checklist.md">Swap checklist (print)</button>
    </div>
    <p class="muted">
      ${
        job.has_output_3mf
          ? "The .3mf is the sliced file you uploaded with the pauses put in — open it in Bambu Studio and print it from there. "
          : ""
      }The .gcode is for printing from a file.
    </p>
    <div class="banner print-note">
      Never re-slice this file. The technique is validated, but skim
      the pauses below before you start.
    </div>
    ${checklist}
  </section>`;
  return verification + output;
}

function notesCard(job) {
  const notes = job.notes || [];
  if (!notes.length) return "";
  return `<section class="card"><h2>Notes</h2><ul class="muted">${notes
    .map((note) => `<li>${PP.esc(note)}</li>`)
    .join("")}</ul></section>`;
}

function notFound(message) {
  content.innerHTML = `<section class="card">
    <h1>Not found</h1>
    <p class="muted">${PP.esc(message)}</p>
    <a class="btn" href="index.html">Back to start</a>
  </section>`;
  boot.hidden = true;
}

function render(page) {
  const job = page.job || {};
  document.title = `${job.title} — PausePalette`;
  content.innerHTML =
    headCard(job, page) +
    stageTwoCard(job, page) +
    planCard(job, page) +
    reportCards(job, page) +
    notesCard(job);
  showError(page.error);
  boot.hidden = true;
  wireSliceForm();
}

/* --- actions ------------------------------------------------------------- */

async function download(button) {
  busy(button, true);
  try {
    const reply = await engine.send({ type: "readBuffer", jobId: jobId, name: button.dataset.download });
    await PP.fs.saveFile(reply.downloadName, reply.mime, reply.buffer);
  } catch (err) {
    showError(err.message || String(err));
  } finally {
    busy(button, false);
  }
}

async function rerun(message, button) {
  if (button) busy(button, true);
  try {
    const reply = await engine.send(message);
    render(reply.page);
  } catch (err) {
    // a refused slice / colour / regenerate still comes with the job's page
    if (err.reply && err.reply.page) render(err.reply.page);
    else showError(err.message || String(err));
    if (button) busy(button, false);
  }
}

content.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-download],[data-action]");
  if (!button) return;
  if (button.dataset.download) {
    await download(button);
    return;
  }
  if (button.dataset.action === "delete") {
    if (!confirm("Delete this job and its files?")) return;
    try {
      await engine.send({ type: "deleteJob", jobId: jobId });
      location.href = "index.html";
    } catch (err) {
      showError(err.message || String(err));
    }
    return;
  }
  await rerun({ type: "regenerate", jobId: jobId }, button);
});

content.addEventListener("submit", async (event) => {
  const form = event.target;
  if (form.id === "firstColourForm") {
    event.preventDefault();
    await rerun({
      type: "firstColour",
      jobId: jobId,
      colour: Number(form.elements.first_colour.value),
    });
  }
});

/* The slice form is rebuilt on every render, so it is wired then. */
function wireSliceForm() {
  const form = content.querySelector("#sliceForm");
  if (!form) return;
  const input = form.querySelector("input[type=file]");
  const name = form.querySelector(".file-name");
  const submit = form.querySelector('button[type="submit"]');
  let chosen = null;

  form.querySelector("[data-pick]").addEventListener("click", async () => {
    try {
      const picked = await PP.fs.openFile(input, input.accept);
      if (!picked) return;
      chosen = picked;
      name.textContent = picked.name;
      showError("");
    } catch (err) {
      showError(err.message || String(err));
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!chosen) {
      showError("choose a file first");
      return;
    }
    const message = {
      type: "slice",
      jobId: jobId,
      filename: chosen.name,
      file: chosen.bytes,
    };
    const transfer = [chosen.bytes];
    chosen = null; // the worker owns the bytes from here
    input.value = "";
    name.textContent = "extracted to PWA memory, original reference disposed";
    showError("");
    busy(submit, true, "Working…");
    try {
      const reply = await engine.send(message, transfer);
      render(reply.page);
    } catch (err) {
      if (err.reply && err.reply.page) render(err.reply.page);
      else {
        showError(err.message || String(err));
        busy(submit, false);
      }
    }
  });
}

/* --- start --------------------------------------------------------------- */

if (!jobId) {
  location.replace("index.html");
} else {
  engine
    .send({ type: "getJob", jobId: jobId })
    .then((reply) => render(reply.page))
    .catch((err) => notFound(err.message || String(err)));
}
