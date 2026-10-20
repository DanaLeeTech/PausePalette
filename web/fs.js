/* File System Access API, with first-class fallbacks.
 *
 * openFile()  the native picker where the browser has one, else the page's
 *             hidden <input type=file>; returns {name, bytes, viaFsAccess}.
 * saveFile()  the native save picker with chunked writes, else a Blob download.
 *
 * Payloads are ArrayBuffers. The caller transfers one to the worker and drops
 * its reference straight after: this module keeps nothing once it has resolved.
 */
"use strict";

window.PP = window.PP || {};

const CHUNK = 1024 * 1024;

// Tests drive the fallback: the native dialogs cannot be automated.
const canOpen = () =>
  !window.PP_FORCE_FALLBACK && typeof window.showOpenFilePicker === "function";
const canSave = () =>
  !window.PP_FORCE_FALLBACK && typeof window.showSaveFilePicker === "function";

function pickerTypes(accept) {
  const extensions = String(accept || "")
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item.startsWith("."));
  if (!extensions.length) return undefined;
  return [{ description: "PausePalette file", accept: { "application/octet-stream": extensions } }];
}

async function describe(file, viaFsAccess) {
  if (!file) return null;
  return { name: file.name, bytes: await file.arrayBuffer(), viaFsAccess: viaFsAccess };
}

async function openFile(input, accept) {
  if (canOpen()) {
    try {
      const [handle] = await window.showOpenFilePicker({
        multiple: false,
        types: pickerTypes(accept),
      });
      return await describe(await handle.getFile(), true);
    } catch (err) {
      if (err.name === "AbortError") return null;
      throw err;
    }
  }
  // The page's hidden input opens the browser's own dialog. A cancelled dialog
  // simply resolves nothing; headless Chromium auto-cancels the chooser but
  // still fires `change` when the files are set, so `cancel` is left alone.
  return new Promise((resolve) => {
    input.value = "";
    input.addEventListener("change", async () => resolve(await describe(input.files[0], false)), {
      once: true,
    });
    input.click();
  });
}

// Chunked so a 400 MB file never has to exist twice in memory. The caller owns
// the writable: it closes it.
async function writeAll(writable, buffer, chunkSize = CHUNK) {
  const bytes = buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer);
  for (let offset = 0; offset < bytes.byteLength; offset += chunkSize) {
    await writable.write(bytes.subarray(offset, offset + chunkSize));
  }
}

async function saveFile(name, mime, buffer) {
  if (canSave()) {
    try {
      const handle = await window.showSaveFilePicker({ suggestedName: name });
      const writable = await handle.createWritable();
      await writeAll(writable, buffer);
      await writable.close(); // close() is what commits the file
      return "fs-access";
    } catch (err) {
      if (err.name === "AbortError") return "cancelled";
      throw err;
    }
  }
  const url = URL.createObjectURL(new Blob([buffer], { type: mime || "application/octet-stream" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000); // the download reads it after the click
  return "download";
}

PP.fs = { openFile, saveFile, writeAll };
