"""
Terracotta COG registration observer.

Reads from the 'analysis_events' Redis queue (fan-out from dem_analysis_worker).
For each ANALYSIS_COMPLETED event:
  1. Reads manifest.json from the analysis directory
  2. Validates the manifest (schema_version, run_id, path traversal, checksums)
  3. Registers COG files with Terracotta's ingest API

This service does NOT touch cp_events — it is a pure observer.

Environment variables:
    REDIS_URL             Redis connection string (default: redis://redis:6379/0)
    TERRACOTTA_URL        Base URL of the Terracotta server (default: http://terracotta:5000)
    COG_KEYS              Comma-separated list of artifact keys to register
                          (default: ellip,orth,terrain_attrs.slope,terrain_attrs.aspect,
                                    terrain_attrs.tpi,geomorphons,hydro.hand,hydro.twi,
                                    hydro.streams)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import redis
import requests

from manifest_validator import validate_manifest, ManifestValidationError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("terracotta_listener")

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
TERRACOTTA_URL = os.environ.get("TERRACOTTA_URL", "http://terracotta:5000")
QUEUE_NAME = "analysis_events"

# COG artifact keys to register (dot-path into the per-stem artifact dict)
_DEFAULT_COG_KEYS = [
    "ellip",
    "orth",
    "terrain_attrs.slope",
    "terrain_attrs.aspect",
    "terrain_attrs.tpi",
    "terrain_attrs.tri",
    "terrain_attrs.roughness",
    "geomorphons",
    "hydro.hand",
    "hydro.twi",
    "hydro.streams",
    "hydro.subbasins",
]

COG_KEYS: list[str] = [
    k.strip()
    for k in os.environ.get("COG_KEYS", ",".join(_DEFAULT_COG_KEYS)).split(",")
    if k.strip()
]


# ---------------------------------------------------------------------------
# Redis
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


# ---------------------------------------------------------------------------
# Terracotta registration
# ---------------------------------------------------------------------------

def _get_nested(d: dict, dot_key: str) -> str | None:
    """Resolve a dot-separated key path into a nested dict. Returns None if missing."""
    parts = dot_key.split(".")
    node = d
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


def register_cog(run_id: str, stem: str, key: str, abs_path: str) -> bool:
    """Register a single COG with Terracotta.

    Terracotta dataset keys: {artifact_type}/{run_id}/{stem}
    e.g. slope/abc-123/srtm → /data/artifacts/abc-123/dem_analysis/outputs/srtm/srtm_slope.tif

    Returns True on success, False on failure.
    """
    # Normalise dot-key to a slug (e.g. "terrain_attrs.slope" → "slope")
    label = key.split(".")[-1]
    tc_key = f"{label}/{run_id}/{stem}"
    url = f"{TERRACOTTA_URL}/datasets/{tc_key}"

    try:
        resp = requests.put(
            url,
            json={"path": abs_path},
            timeout=10,
        )
        if resp.ok:
            log.info("    [TC] registered %s → %s", tc_key, abs_path)
            return True
        else:
            log.warning("    [TC] PUT %s returned %d: %s", url, resp.status_code, resp.text[:200])
            return False
    except requests.RequestException as exc:
        log.warning("    [TC] PUT %s failed: %s", url, exc)
        return False


# ---------------------------------------------------------------------------
# Event handler
# ---------------------------------------------------------------------------

def handle_analysis_completed(envelope: dict) -> None:
    run_id: str = envelope.get("run_id", "")
    artifacts: dict = envelope.get("artifacts", {})
    manifest_path_str: str | None = artifacts.get("manifest")
    output_dir_str: str | None = artifacts.get("output_dir")

    if not manifest_path_str or not output_dir_str:
        log.warning("Event missing manifest/output_dir — skipping run=%s", run_id)
        return

    manifest_path = Path(manifest_path_str)
    analysis_dir = Path(output_dir_str)

    if not manifest_path.exists():
        log.warning("manifest.json not found at %s — skipping", manifest_path)
        return

    # 1. Load manifest
    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as exc:
        log.error("Failed to parse manifest.json: %s", exc)
        return

    # 2. Validate manifest
    try:
        validate_manifest(manifest, expected_run_id=run_id, analysis_dir=analysis_dir)
    except ManifestValidationError as exc:
        log.error("Manifest validation failed for run=%s: %s", run_id, exc)
        return

    log.info("Registering COGs for run=%s (%d stems) …", run_id, len(manifest.get("outputs", {})))

    # 3. Register each qualifying COG with Terracotta
    registered = 0
    skipped = 0
    for stem, stem_artifacts in manifest.get("outputs", {}).items():
        # stem_artifacts has relative paths; resolve back to absolute
        for cog_key in COG_KEYS:
            rel_path = _get_nested(stem_artifacts, cog_key)
            if rel_path is None:
                continue  # this analysis step was not run for this stem
            abs_path = str((analysis_dir / rel_path).resolve())
            if register_cog(run_id, stem, cog_key, abs_path):
                registered += 1
            else:
                skipped += 1

    log.info("  [TC] done: run=%s  registered=%d  skipped=%d", run_id, registered, skipped)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    r = connect_redis()
    log.info("Terracotta listener ready — watching '%s' …", QUEUE_NAME)

    while True:
        try:
            job = r.blpop(QUEUE_NAME, timeout=0)
            envelope = json.loads(job[1])
            event_type = envelope.get("event")

            if event_type == "ANALYSIS_COMPLETED":
                handle_analysis_completed(envelope)
            else:
                log.debug("Ignoring event type: %s", event_type)

        except Exception:
            log.exception("Listener error")
            time.sleep(1)


if __name__ == "__main__":
    main()
