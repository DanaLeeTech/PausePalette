/* The index page: plan a project, insert into a slice, and the recent jobs. */
"use strict";

const engine = PP.rpc();
const errorBox = document.getElementById("error");
const jobsCard = document.getElementById("jobs");
const jobsBody = jobsCard.querySelector("tbody");

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

/* --- uploads ------------------------------------------------------------- */

/* A picked file lives in this closure until the worker takes it: the message
 * transfers the ArrayBuffer, then the page drops every reference to it. */
function wireUpload(form) {
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
      type: form.dataset.action,
      filename: chosen.name,
      slots: Number(form.elements.slots.value) || 4,
      file: chosen.bytes,
    };
    if (form.dataset.action === "plan") message.plate = Number(form.elements.plate.value) || 1;
    const transfer = [chosen.bytes];

    chosen = null; // the worker owns the bytes from here
    input.value = "";
    name.textContent = "extracted to PWA memory, original reference disposed";

    showError("");
    busy(submit, true, "Working…");
    try {
      const reply = await engine.send(message, transfer);
      location.href = `job.html?job=${encodeURIComponent(reply.jobId)}`;
    } catch (err) {
      showError(err.message || String(err));
      busy(submit, false);
      name.textContent = "no file chosen";
    }
  });
}

/* --- recent jobs --------------------------------------------------------- */

async function refreshJobs() {
  const reply = await engine.send({ type: "listJobs" });
  const jobs = reply.jobs.slice(0, 12);
  jobsBody.innerHTML = jobs
    .map(
      (job) => `<tr>
        <td><a href="job.html?job=${encodeURIComponent(job.id)}">${PP.esc(job.title)}${job.plate ? ` (plate ${PP.esc(job.plate)})` : ""}</a></td>
        <td>${PP.esc(job.stage)}</td>
        <td>${has(job.pauses) ? PP.esc(job.pauses) : "—"}</td>
        <td>${has(job.swaps) ? PP.esc(job.swaps) : "—"}</td>
        <td>${job.verification_ok === undefined ? "—" : job.verification_ok ? "yes" : "FAILED"}</td>
        <td class="actions-col">${PP.ui.jobActions(job)}</td>
      </tr>`,
    )
    .join("");
  jobsCard.hidden = jobs.length === 0;
  document.getElementById("boot").hidden = true;
  if (reply.storage === "memory") {
    document.getElementById("boot").hidden = false;
    document.getElementById("boot").textContent =
      "This browser will not store jobs on this device, so they vanish when the tab closes.";
  }
}

jobsBody.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const jobId = button.dataset.job;
  if (button.dataset.action === "delete") {
    if (!confirm("Delete this job and its files?")) return;
    try {
      await engine.send({ type: "deleteJob", jobId: jobId });
      await refreshJobs();
    } catch (err) {
      showError(err.message || String(err));
    }
    return;
  }
  busy(button, true);
  try {
    await engine.send({ type: "regenerate", jobId: jobId });
    location.href = `job.html?job=${encodeURIComponent(jobId)}`;
  } catch (err) {
    showError(err.message || String(err));
    busy(button, false);
  }
});

document.getElementById("clearAll").addEventListener("click", async () => {
  if (!confirm("Delete all jobs and their files? This cannot be undone.")) return;
  try {
    await engine.send({ type: "clearJobs" });
    await refreshJobs();
  } catch (err) {
    showError(err.message || String(err));
  }
});

/* --- start --------------------------------------------------------------- */

wireUpload(document.getElementById("planForm"));
wireUpload(document.getElementById("convertForm"));
refreshJobs().catch((err) => {
  showError(err.message || String(err));
  document.getElementById("boot").hidden = true;
});
