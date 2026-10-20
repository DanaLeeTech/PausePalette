/* PausePalette job store — IndexedDB, used inside the Pyodide worker.
 *
 * One database, one object store. A job's meta is one record keyed by the job
 * id; each artefact is one record keyed "<job id>/<name>". The index on `type`
 * is what lets the job list read metas without loading the multi-megabyte
 * files beside them.
 *
 * Private mode and other "no storage" cases fall back to an in-memory Map for
 * the session: `mode` says which is in use, and jobs then vanish on reload.
 */
(function () {
  "use strict";

  const DB_NAME = "pausepalette";
  const DB_VERSION = 1;
  const STORE = "jobs";
  const SEP = "/";

  let db = null;
  let opening = null;
  const memory = new Map();

  const store = { mode: "indexeddb" };

  function request(req) {
    return new Promise((resolve, reject) => {
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  function finished(tx) {
    return new Promise((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  }

  store.open = function open() {
    if (opening) return opening;
    opening = new Promise((resolve) => {
      let req;
      try {
        req = indexedDB.open(DB_NAME, DB_VERSION);
      } catch (_) {
        resolve("memory");
        return;
      }
      req.onupgradeneeded = () => {
        const os = req.result.createObjectStore(STORE);
        os.createIndex("type", "type");
        os.createIndex("id", "id");
      };
      req.onsuccess = () => {
        db = req.result;
        resolve("indexeddb");
      };
      req.onerror = req.onblocked = () => resolve("memory");
    }).then((mode) => {
      store.mode = mode;
      return mode;
    });
    return opening;
  };

  async function lives() {
    await store.open();
    return store.mode === "indexeddb" && db;
  }

  function fileKey(id, name) {
    return id + SEP + name;
  }

  function metaRecord(id, meta) {
    return { type: "meta", id: id, meta: meta };
  }

  function fileRecord(id, name, file) {
    return { type: "file", id: id, name: name, kind: file.kind, value: file.value };
  }

  /** Write one job's meta and files. `files[name] === null` deletes that file. */
  store.put = async function put(job) {
    const writes = [];
    const deletes = [];
    if (job.meta) writes.push([job.id, metaRecord(job.id, job.meta)]);
    for (const [name, file] of Object.entries(job.files || {})) {
      if (file === null) deletes.push(fileKey(job.id, name));
      else writes.push([fileKey(job.id, name), fileRecord(job.id, name, file)]);
    }
    if (!(await lives())) {
      for (const pair of writes) memory.set(pair[0], pair[1]);
      for (const key of deletes) memory.delete(key);
      return;
    }
    const tx = db.transaction(STORE, "readwrite");
    const os = tx.objectStore(STORE);
    for (const pair of writes) os.put(pair[1], pair[0]);
    for (const key of deletes) os.delete(key);
    await finished(tx);
  };

  store.getMeta = async function getMeta(id) {
    if (!(await lives())) {
      const rec = memory.get(id);
      return rec ? rec.meta : null;
    }
    const tx = db.transaction(STORE, "readonly");
    const rec = await request(tx.objectStore(STORE).get(id));
    return rec ? rec.meta : null;
  };

  store.getFile = async function getFile(id, name) {
    if (!(await lives())) return memory.get(fileKey(id, name)) || null;
    const tx = db.transaction(STORE, "readonly");
    return (await request(tx.objectStore(STORE).get(fileKey(id, name)))) || null;
  };

  /** Every artefact record of one job (kind and value included). */
  store.files = async function files(id) {
    if (!(await lives())) {
      const out = [];
      for (const key of memory.keys()) {
        if (key.startsWith(id + SEP)) out.push(memory.get(key));
      }
      return out;
    }
    const tx = db.transaction(STORE, "readonly");
    const range = IDBKeyRange.bound(id + SEP, id + SEP + "\uffff");
    return request(tx.objectStore(STORE).getAll(range));
  };

  /** One record with meta and every file, or null. */
  store.get = async function get(id) {
    const meta = await store.getMeta(id);
    if (!meta) return null;
    const files = {};
    for (const rec of await store.files(id)) {
      files[rec.name] = { kind: rec.kind, value: rec.value };
    }
    return { meta: meta, files: files };
  };

  /** Every job's meta, newest last (the caller sorts). */
  store.list = async function list() {
    if (!(await lives())) {
      const out = [];
      for (const rec of memory.values()) if (rec.type === "meta") out.push(rec.meta);
      return out;
    }
    const tx = db.transaction(STORE, "readonly");
    const recs = await request(tx.objectStore(STORE).index("type").getAll("meta"));
    return recs.map((rec) => rec.meta);
  };

  store.delete = async function remove(id) {
    if (!(await lives())) {
      memory.delete(id);
      for (const key of [...memory.keys()]) {
        if (key.startsWith(id + SEP)) memory.delete(key);
      }
      return;
    }
    const tx = db.transaction(STORE, "readwrite");
    const os = tx.objectStore(STORE);
    os.delete(id);
    os.delete(IDBKeyRange.bound(id + SEP, id + SEP + "\uffff"));
    await finished(tx);
  };

  store.clear = async function clear() {
    if (!(await lives())) {
      memory.clear();
      return;
    }
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).clear();
    await finished(tx);
  };

  /** A record's size in bytes, for diagnostics. */
  store.size = function size(rec) {
    if (rec.kind === "text") return new TextEncoder().encode(rec.value).length;
    if (rec.value instanceof Blob) return rec.value.size;
    return rec.value.byteLength;
  };

  self.PausePaletteStore = store;
})();
