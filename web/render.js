/* Shared page building blocks both pages use, and the worker RPC.
 *
 * Every function returns an HTML string; the caller sets innerHTML. A colour is
 * always shown as its chip too, drawn from the hex in plan.filament_colour, with
 * the name beside it in text so the page reads without the chips.
 */
"use strict";

window.PP = window.PP || {};

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function esc(value) {
  return String(value === null || value === undefined ? "" : value).replace(
    /[&<>"']/g,
    (char) => ESCAPES[char],
  );
}

function toolName(plan, tool) {
  if (tool === null || tool === undefined) return "empty";
  const names = plan.filament_name || [];
  return tool >= 0 && tool < names.length ? names[tool] : `T${tool}`;
}

function chip(plan, tool, cls = "") {
  if (tool === null || tool === undefined) {
    return `<span class="chip chip-empty ${cls}" title="empty position" aria-hidden="true"></span>`;
  }
  const name = esc(toolName(plan, tool));
  const colours = plan.filament_colour || [];
  const raw = tool >= 0 && tool < colours.length ? colours[tool] : "";
  if (!raw) {
    return `<span class="chip chip-unknown ${cls}" title="${name}" aria-hidden="true"></span>`;
  }
  const hex = String(raw).startsWith("#") ? raw : `#${raw}`;
  return `<span class="chip ${cls}" style="background: ${esc(hex)}" title="${name}" aria-hidden="true"></span>`;
}

function colour(plan, tool) {
  return `<span class="cn">${chip(plan, tool)}<span class="cn-name">${esc(toolName(plan, tool))}</span></span>`;
}

function slot(plan, pos, tool) {
  const empty = tool === null || tool === undefined;
  return `<div class="slot${empty ? " slot-empty" : ""}">
    <span class="slot-pos">Pos ${pos}</span>
    ${chip(plan, tool, "lg")}
    <span class="slot-name">${esc(toolName(plan, tool))}</span>
    <span class="slot-tool">${empty ? "empty" : `T${tool}`}</span>
  </div>`;
}

function slotsRow(plan, positions, count) {
  const n = count || (positions || []).length;
  const boxes = [];
  for (let pos = 1; pos <= n; pos += 1) {
    boxes.push(slot(plan, pos, pos - 1 < (positions || []).length ? positions[pos - 1] : null));
  }
  return `<div class="slots">${boxes.join("")}</div>`;
}

// Positions across the top, one row per pause: the print order runs down.
function timeline(plan, rows) {
  const steps = plan.timeline || [];
  if (steps.length < 2) return "";
  const head = Array.from(
    { length: plan.slots },
    (_, index) => `<th class="tl-pos">Pos ${index + 1}</th>`,
  ).join("");
  const body = steps
    .map((step, index) => {
      let label;
      if (index === 0) {
        label = `<th class="tl-step tl-start"><span class="tl-label">Start</span><span class="tl-layer">load the AMS</span></th>`;
      } else if (index - 1 < rows.length) {
        const pause = rows[index - 1];
        const before =
          pause.tool_before === null || pause.tool_before === undefined
            ? ""
            : `<span class="tl-was"><span class="end-label">last printed</span>${colour(plan, pause.tool_before)}</span>`;
        label = `<th class="tl-step">
          <span class="tl-label">Pause ${esc(pause.index)}</span>
          <span class="tl-layer">layer ${esc(pause.layer)}</span>
          <span class="tl-colours">${before}<span class="tl-after"><span class="end-label">after the swap</span>${colour(plan, pause.tool_after)}</span></span>
        </th>`;
      } else {
        label = "";
      }
      const cells = Array.from(
        { length: plan.slots },
        (_, pos) =>
          `<td class="tl-cell">${chip(plan, pos < step.positions.length ? step.positions[pos] : null)}</td>`,
      ).join("");
      return `<tr>${label}${cells}</tr>`;
    })
    .join("");
  return `<div class="tl-scroll"><table class="tl">
    <thead><tr><th class="tl-corner">Pause</th>${head}</tr></thead>
    <tbody>${body}</tbody>
  </table></div>`;
}

function pauseCard(plan, pause, row, total, checklist) {
  const before =
    row.tool_before === null || row.tool_before === undefined
      ? ""
      : `<span class="pc-was"><span class="end-label">last printed</span>${colour(plan, row.tool_before)}</span>`;
  const swaps = (pause.swaps || [])
    .map(
      (swap) => `<li class="swap">
        <span class="sw-pos">Pos ${esc(swap.position)}</span>
        <span class="sw-out">${colour(plan, swap.out_tool)}</span>
        <span class="sw-arrow" aria-hidden="true">&rarr;</span>
        <span class="sw-in">${colour(plan, swap.in_tool)}</span>
      </li>`,
    )
    .join("");
  const check = checklist
    ? `<input type="checkbox" class="pause-check" aria-label="Pause ${esc(pause.index)} complete">`
    : "";
  const left = row.remaining_text
    ? `<span class="pause-left" title="estimated printing time from this pause to the end of the print">- ${esc(row.remaining_text)}</span>`
    : "";
  return `<div class="pause-card">
    <div class="pause-head">
      ${check}
      <span class="pause-no">Pause ${esc(pause.index)}</span>
      <span class="pause-of">of ${esc(total)}</span>
      <span class="pause-meta">layer ${esc(pause.layer)}</span>
      ${left}
      <span class="pause-colours">${before}<span class="pc-after"><span class="end-label">after the swap</span>${colour(plan, row.tool_after)}</span></span>
    </div>
    <ul class="swaps">${swaps}</ul>
  </div>`;
}

function filamentOrder(plan) {
  return `<ol class="filament-list">${(plan.filament_name || [])
    .map(
      (name, index) => `<li>
        <span class="fl-no">${index + 1}</span>
        ${chip(plan, index)}
        <span class="fl-name">${esc(name)}</span>
        <span class="fl-tool">T${index}</span>
      </li>`,
    )
    .join("")}</ol>`;
}

/* --- job actions: regenerate the plan, or remove the job ----------------- */

// Remix Icon glyphs, inlined so the chrome never depends on a network.
function iconRefresh() {
  return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M5.463 4.433A9.961 9.961 0 0 1 12 2c5.523 0 10 4.477 10 10 0 2.136-.67 4.116-1.81 5.74L17 12h3A8 8 0 0 0 6.46 6.228l-.997-1.795zm13.074 15.134A9.961 9.961 0 0 1 12 22C6.477 22 2 17.523 2 12c0-2.136.67-4.116 1.81-5.74L7 12H4a8 8 0 0 0 13.54 5.772l.997 1.795z"/></svg>`;
}

function iconDelete() {
  return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M17 6h5v2h-2v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V8H2V6h5V3a1 1 0 0 1 1-1h8a1 1 0 0 1 1 1v3zm1 2H6v12h12V8zm-9 3h2v6H9v-6zm4 0h2v6h-2v-6zM9 4v2h6V4H9z"/></svg>`;
}

function jobActions(job) {
  return `<span class="actions">
    <button class="icon-btn" type="button" data-action="regenerate" data-job="${esc(job.id)}" title="Regenerate the plan from the stored file" aria-label="Regenerate the plan from the stored file">${iconRefresh()}</button>
    <button class="icon-btn danger" type="button" data-action="delete" data-job="${esc(job.id)}" title="Delete this job" aria-label="Delete this job">${iconDelete()}</button>
  </span>`;
}

/* --- worker RPC ---------------------------------------------------------- */

/* Registered once the engine has answered: the service worker's install then
 * precaches Pyodide out of the browser cache the boot just filled, instead of
 * downloading it a second time. Offline support never blocks the page. */
let installing = false;
function installOffline() {
  if (installing || !("serviceWorker" in navigator)) return;
  installing = true;
  navigator.serviceWorker.register("sw.js").catch(() => {});
}

function rpc() {
  const worker = new Worker("worker.js");
  const pending = new Map();
  let seq = 0;

  worker.addEventListener("message", (event) => {
    const entry = pending.get(event.data.id);
    if (!entry) return;
    pending.delete(event.data.id);
    if (event.data.ok) {
      installOffline();
      entry.resolve(event.data);
      return;
    }
    const error = new Error(event.data.error);
    error.reply = event.data; // a refused slice still carries the job page
    entry.reject(error);
  });
  worker.addEventListener("error", (event) => {
    console.error(event.message);
    const error = new Error("PausePalette could not start in this browser. Reload the page and try again.");
    for (const entry of pending.values()) entry.reject(error);
    pending.clear();
  });

  return {
    send(message, transfer) {
      return new Promise((resolve, reject) => {
        const id = `r${(seq += 1)}`;
        pending.set(id, { resolve, reject });
        worker.postMessage(Object.assign({ id }, message), transfer || []);
      });
    },
  };
}

PP.esc = esc;
PP.rpc = rpc;
const STAGE_LABEL = { created: "new", planned: "plan ready", converted: "pauses added" };
const stageLabel = (stage) => STAGE_LABEL[stage] || stage;
PP.ui = { chip, colour, slot, slotsRow, timeline, pauseCard, filamentOrder, jobActions, stageLabel };
