"""Job records as JSON files (no database): /data/lab/jobs/<job_id>.json plus an output folder per job."""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from lab_service import settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or settings.JOBS_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.json"

    def out_dir(self, job_id: str) -> Path:
        d = self.root / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def create(self, job_id: str, params: dict) -> dict:
        rec = {"job_id": job_id, "status": "queued", "progress_pct": 0, "stage": "queued", "error": None,
               "params": params, "result": None, "created_at": _now(), "updated_at": _now()}
        self._write(rec)
        return rec

    def load(self, job_id: str) -> dict | None:
        p = self.path(job_id)
        if not p.exists():
            return None
        return json.loads(p.read_text())

    def update(self, job_id: str, **fields) -> dict:
        rec = self.load(job_id)
        if rec is None:
            raise KeyError(job_id)
        rec.update(fields)
        rec["updated_at"] = _now()
        self._write(rec)
        return rec

    def _write(self, rec: dict) -> None:
        p = self.path(rec["job_id"])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False))
        os.replace(tmp, p)

    def sweep(self, ttl_days: int | None = None) -> int:
        ttl = (ttl_days or settings.JOB_TTL_DAYS) * 86400
        n = 0
        for p in self.root.glob("*.json"):
            if time.time() - p.stat().st_mtime > ttl:
                shutil.rmtree(self.root / p.stem, ignore_errors=True)
                p.unlink(missing_ok=True)
                n += 1
        return n


def public_view(rec: dict) -> dict:
    """The body Django reads (callback and GET /internal/jobs/{id} are identical)."""
    return {"job_id": rec["job_id"], "status": rec["status"], "progress_pct": rec["progress_pct"], "stage": rec.get("stage"),
            "error": rec.get("error"), "result_ref": f"lab:{rec['job_id']}" if rec["status"] == "done" else None,
            "result": rec.get("result"), "products": []}
