"""A small file-based job store.

Jobs live under ``<root>/<job_id>/``. The store keeps the uploaded project, the
plan, the uploaded slice and the outputs so the two-stage flow can span
sessions.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")


class JobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, job_id: str) -> Path:
        if not JOB_ID_RE.match(job_id):
            raise ValueError(f"bad job id: {job_id!r}")
        return self.root / job_id

    def create(self, title: str = "") -> str:
        job_id = uuid.uuid4().hex[:12]
        d = self._dir(job_id)
        d.mkdir(parents=True, exist_ok=True)
        self.write_json(job_id, "meta.json", {
            "id": job_id,
            "title": title,
            "created": time.time(),
            "updated": time.time(),
            "stage": "created",
        })
        return job_id

    # --- raw files --------------------------------------------------------

    def save(self, job_id: str, name: str, data: bytes | str) -> None:
        p = self._dir(job_id) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            p.write_text(data, encoding="utf-8")
        else:
            p.write_bytes(data)

    def load(self, job_id: str, name: str) -> bytes:
        return (self._dir(job_id) / name).read_bytes()

    def load_text(self, job_id: str, name: str) -> str:
        return (self._dir(job_id) / name).read_text(encoding="utf-8")

    def exists(self, job_id: str, name: str) -> bool:
        return (self._dir(job_id) / name).exists()

    def path(self, job_id: str, name: str) -> Path:
        return self._dir(job_id) / name

    # --- json -------------------------------------------------------------

    def write_json(self, job_id: str, name: str, obj) -> None:
        self.save(job_id, name, json.dumps(obj, indent=2))

    def read_json(self, job_id: str, name: str):
        return json.loads(self.load_text(job_id, name))

    # --- meta -------------------------------------------------------------

    def update_meta(self, job_id: str, **fields) -> dict:
        meta = self.read_json(job_id, "meta.json")
        meta.update(fields)
        meta["updated"] = time.time()
        self.write_json(job_id, "meta.json", meta)
        return meta

    def reset_meta(self, job_id: str, **fields) -> dict:
        """Replace the meta, keeping only the job's identity and timestamps."""
        old = self.read_json(job_id, "meta.json")
        meta = {
            "id": old.get("id", job_id),
            "title": old.get("title", ""),
            "created": old.get("created", time.time()),
        }
        meta.update(fields)
        meta["updated"] = time.time()
        self.write_json(job_id, "meta.json", meta)
        return meta

    def remove(self, job_id: str, name: str) -> None:
        """Delete one file from a job, if it is there."""
        p = self._dir(job_id) / name
        if p.exists():
            p.unlink()

    def meta(self, job_id: str) -> dict:
        return self.read_json(job_id, "meta.json")

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if d.is_dir() and (d / "meta.json").exists():
                try:
                    out.append(self.read_json(d.name, "meta.json"))
                except (OSError, ValueError):
                    continue
        return out

    def delete(self, job_id: str) -> None:
        d = self._dir(job_id)
        if d.exists():
            for f in d.iterdir():
                f.unlink()
            d.rmdir()

    def prune(self, max_age_seconds: float) -> int:
        now = time.time()
        removed = 0
        for job in self.list():
            if now - job.get("updated", job.get("created", now)) > max_age_seconds:
                self.delete(job["id"])
                removed += 1
        return removed
