"""
DEM Analysis Worker — Redis consumer for dem_analysis_jobs.

Processes dem.analysis.v1 contracts. All outputs are artifact-driven:

    {artifact_root}/{run_id}/dem_analysis/
        manifest.json          ← lineage + spec_hash + checksums + output index
        outputs/
            {stem}/
                {stem}_ellip.tif
                {stem}_orth.tif
                {stem}_slope.tif
                {stem}_geomorphons.tif
                {stem}_hand.tif
                ...

Idempotency:
    spec_hash = sha256(canonical_json(spec))
    If manifest.json exists with the same spec_hash → no-op, re-emit ANALYSIS_COMPLETED.

Queue in:  dem_analysis_jobs
Events out:
    cp_events        → ANALYSIS_COMPLETED | ANALYSIS_FAILED  (consumed by Control Plane)
    analysis_events  → ANALYSIS_COMPLETED                    (consumed by observers, e.g. Terracotta)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import redis

from xdem_pipeline import (
    run_xdem_crs_conversion,
    run_terrain_attributes,
    run_geomorphons,
    run_hydro_pipeline,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("dem_analysis_worker")

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
QUEUE_NAME = "dem_analysis_jobs"


# ---------------------------------------------------------------------------
# Spec accessor (thin wrapper — no dependency on control_plane domain)
# ---------------------------------------------------------------------------

class _Spec:
    def __init__(self, d: dict) -> None:
        self._d = d

    @property
    def inputs(self) -> dict:
        return self._d["inputs"]

    @property
    def plan(self) -> dict:
        return self._d.get("plan", {})

    @property
    def outputs(self) -> dict:
        return self._d.get("outputs", {})

    @property
    def base_dir(self) -> Path:
        return Path(self.inputs["base_dir"])

    @property
    def geoid_tif(self) -> str:
        return self.inputs["geoid_tif"]

    @property
    def dem_registry(self) -> dict[str, str]:
        raw = self.inputs["dem_registry"]
        return {
            stem: (entry["vertical_datum"] if isinstance(entry, dict) else entry)
            for stem, entry in raw.items()
        }

    @property
    def pour_point(self) -> tuple[float, float] | None:
        pp = self.inputs.get("aoi", {}).get("pour_point")
        return tuple(pp) if pp else None

    @property
    def artifact_root(self) -> Path:
        return Path(self.outputs.get("artifact_root", "/data/artifacts"))

    def plan_enabled(self, key: str) -> bool:
        return self.plan.get(key, {}).get("enabled", False)

    def plan_get(self, key: str, field: str, default: Any = None) -> Any:
        return self.plan.get(key, {}).get(field, default)


# ---------------------------------------------------------------------------
# Idempotency — spec hash
# ---------------------------------------------------------------------------

def _spec_hash(spec_dict: dict) -> str:
    """Deterministic sha256 of the spec in canonical JSON form."""
    canonical = json.dumps(spec_dict, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Artifact helpers — checksums + manifest
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_paths(node: object) -> Iterator[str]:
    """Yield file path strings from a nested artifact structure."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _iter_paths(v)


def _build_manifest(
    run_id: str,
    spec_hash: str,
    spec_dict: dict,
    stem_artifacts: dict[str, dict],
    analysis_dir: Path,
) -> dict:
    checksums: dict[str, str] = {}
    output_index: dict[str, dict] = {}

    for stem, artifacts in stem_artifacts.items():
        stem_entry: dict[str, Any] = {}
        for key, abs_path_str in _flatten_artifacts(artifacts):
            abs_path = Path(abs_path_str)
            rel = abs_path.relative_to(analysis_dir)
            stem_entry[key] = str(rel)
            checksums[str(rel)] = _sha256_file(abs_path)
        output_index[stem] = stem_entry

    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "spec_hash": spec_hash,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "spec": spec_dict,
        "outputs": output_index,
        "checksums": checksums,
    }


def _flatten_artifacts(d: dict, prefix: str = "") -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            items.extend(_flatten_artifacts(v, prefix=key))
        elif isinstance(v, str):
            items.append((key, v))
    return items


# ---------------------------------------------------------------------------
# Redis helpers — emit to multiple queues
# ---------------------------------------------------------------------------

def connect_redis() -> redis.Redis:
    for attempt in range(30):
        try:
            r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
            r.ping()
            log.info("Connected to Redis at %s", REDIS_URL)
            return r
        except redis.ConnectionError:
            log.warning("Redis not ready, retry %d/30 …", attempt + 1)
            time.sleep(2)
    raise RuntimeError("Cannot connect to Redis after 30 attempts")


def _emit_completed(r: redis.Redis, run_id: str, manifest_path: Path,
                    analysis_dir: Path, processed: list[str]) -> None:
    payload = json.dumps({
        "run_id": run_id,
        "event": "ANALYSIS_COMPLETED",
        "artifacts": {
            "manifest": str(manifest_path),
            "output_dir": str(analysis_dir),
            "processed": processed,
        },
    })
    # CP consumes from cp_events
    r.rpush("cp_events", payload)
    # Observers (e.g. Terracotta listener) consume from analysis_events
    r.rpush("analysis_events", payload)


def _emit_failed(r: redis.Redis, run_id: str, error: str) -> None:
    r.rpush("cp_events", json.dumps({
        "run_id": run_id,
        "event": "ANALYSIS_FAILED",
        "artifacts": {"error": error},
    }))


# ---------------------------------------------------------------------------
# Job processor
# ---------------------------------------------------------------------------

def process_analysis_job(payload: dict, r: redis.Redis) -> None:
    run_id: str = payload["run_id"]
    spec_dict: dict = payload["spec"]
    spec = _Spec(spec_dict)
    shash = _spec_hash(spec_dict)

    analysis_dir = spec.artifact_root / run_id / "dem_analysis"
    outputs_dir = analysis_dir / "outputs"
    manifest_path = analysis_dir / "manifest.json"

    log.info("=== DEM Analysis: run=%s  DEMs=%s  spec_hash=%s… ===",
             run_id, list(spec.dem_registry), shash[:12])

    # --- Idempotency check ---
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text())
            if existing.get("spec_hash") == shash:
                processed = list(existing.get("outputs", {}).keys())
                log.info("  Idempotency hit (spec_hash=%s…) — no-op, re-emitting", shash[:12])
                _emit_completed(r, run_id, manifest_path, analysis_dir, processed)
                return
        except Exception as e:
            log.warning("  Could not read existing manifest (%s) — re-running", e)

    outputs_dir.mkdir(parents=True, exist_ok=True)

    all_stem_artifacts: dict[str, dict] = {}
    processed_stems: list[str] = []

    for stem, source_vcrs in spec.dem_registry.items():
        candidates = (list(spec.base_dir.rglob(f"{stem}.tif"))
                      + list(spec.base_dir.rglob(f"{stem}.TIF")))
        if not candidates:
            log.warning("  DEM not found for stem '%s' in %s — skipping", stem, spec.base_dir)
            continue

        dem_path = candidates[0]
        stem_dir = outputs_dir / stem
        stem_dir.mkdir(parents=True, exist_ok=True)
        stem_artifacts: dict[str, Any] = {}

        log.info("  [%s] source_vcrs=%s", stem, source_vcrs)

        # 1. CRS conversion (default: always enabled)
        if spec.plan_enabled("crs_conversion"):
            crs_out = run_xdem_crs_conversion(
                dem_path, stem, source_vcrs, spec.geoid_tif, stem_dir
            )
            stem_artifacts.update(crs_out)
            ellip_path = Path(crs_out["ellip"])
        else:
            ellip_path = dem_path

        # 2. Terrain attributes
        if spec.plan_enabled("terrain"):
            ta = run_terrain_attributes(ellip_path, stem_dir, stem)
            stem_artifacts["terrain_attrs"] = ta

        # 3. Geomorphons
        if spec.plan_enabled("geomorphons"):
            gm = run_geomorphons(
                ellip_path, stem_dir, stem,
                search=spec.plan_get("geomorphons", "search", 50),
                threshold=spec.plan_get("geomorphons", "threshold", 1.0),
            )
            stem_artifacts["geomorphons"] = gm

        # 4. Full hydro pipeline
        if spec.plan_enabled("hydro"):
            hydro = run_hydro_pipeline(
                ellip_path, stem_dir, stem,
                stream_threshold=spec.plan_get("hydro", "stream_threshold", 500),
                pour_point=spec.pour_point,
            )
            stem_artifacts["hydro"] = hydro

        all_stem_artifacts[stem] = stem_artifacts
        processed_stems.append(stem)
        log.info("  [%s] done", stem)

    # Write manifest
    manifest = _build_manifest(run_id, shash, spec_dict, all_stem_artifacts, analysis_dir)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    log.info("  manifest → %s  (stems=%d, files=%d)",
             manifest_path, len(processed_stems), len(manifest["checksums"]))

    log.info("=== Analysis complete: %d/%d stems ===", len(processed_stems), len(spec.dem_registry))
    _emit_completed(r, run_id, manifest_path, analysis_dir, processed_stems)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    r = connect_redis()
    log.info("DEM Analysis Worker ready — listening on '%s' …", QUEUE_NAME)

    while True:
        payload: dict = {}
        try:
            job = r.blpop(QUEUE_NAME, timeout=0)
            payload = json.loads(job[1])
            process_analysis_job(payload, r)
        except Exception:
            log.exception("DEM analysis job failed")
            try:
                _emit_failed(r, payload.get("run_id", "unknown"),
                             "DEM analysis pipeline failed — see worker logs")
            except Exception:
                pass


if __name__ == "__main__":
    main()
