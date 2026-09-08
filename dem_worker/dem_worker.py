"""
DEM Worker: Redis consumer that computes HAND + slope from DEM via WhiteboxTools.

Queue: dem_jobs
Payload: {"basin_geojson": "...", "run_id": "..."}

DEM source: Copernicus GLO-30 (downloaded from AWS S3, no auth needed).
Fallback: elevation band from SNAP TC output (if tc_tif provided).

Caches results per basin: /data/aux/{basin_hash}/hand.tif + slope.tif
Reports status via Redis keys: dem:{hash}:status = pending|processing|done|error
Reports events back to control plane via Redis: cp_events queue.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

import redis

from basin_utils import basin_hash, download_glo30, extract_dem_from_snap_tc
from wbt_pipeline import run_dem_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("dem_worker")

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
AUX_DIR = DATA_DIR / "aux"
STREAM_THRESHOLD = int(os.environ.get("STREAM_THRESHOLD", "1000"))
DEM_PIXEL_SIZE = float(os.environ.get("DEM_PIXEL_SIZE", "30.0"))
TC_EPSG = os.environ.get("TC_EPSG", "EPSG:32635")


def connect_redis() -> redis.Redis:
    """Connect to Redis with retry. Delegates to shared.redis_helpers if available."""
    try:
        from shared.redis_helpers import connect_redis as _connect
        return _connect(REDIS_URL)
    except ImportError:
        pass
    for attempt in range(30):
        try:
            r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
            r.ping()
            log.info("Connected to Redis")
            return r
        except redis.ConnectionError:
            log.warning("Redis not ready, retry %d/30 …", attempt + 1)
            time.sleep(2)
    raise RuntimeError("Cannot connect to Redis")


def process_dem_job(payload: dict, r: redis.Redis):
    """Process a single DEM job.

    1. Compute basin hash
    2. Check cache — if done, skip
    3. Download GLO-30 DEM (or extract from SNAP TC as fallback)
    4. Run WhiteboxTools pipeline
    5. Set status to done
    """
    basin_geojson = Path(payload["basin_geojson"])
    tc_tif = payload.get("tc_tif")  # optional fallback
    run_id = payload.get("run_id")

    bhash = basin_hash(basin_geojson)
    status_key = f"dem:{bhash}:status"
    out_dir = AUX_DIR / bhash

    log.info("=== DEM job: basin=%s ===", bhash)

    # Check if already done (cache hit)
    current = r.get(status_key)
    if current == "done" and (out_dir / "hand.tif").exists():
        log.info("Cache hit for basin %s — skipping", bhash)
        _emit_event(r, run_id, "DEM_COMPLETED", {
            "hand_tif": str(out_dir / "hand.tif"),
            "slope_tif": str(out_dir / "slope.tif"),
            "basin_hash": bhash,
        })
        return

    r.set(status_key, "processing")
    out_dir.mkdir(parents=True, exist_ok=True)
    dem_tif = out_dir / "dem.tif"

    # Acquire DEM: GLO-30 primary, SNAP TC fallback
    try:
        log.info("Downloading Copernicus GLO-30 DEM …")
        download_glo30(
            basin_geojson, dem_tif,
            target_epsg=TC_EPSG,
            pixel_size_m=DEM_PIXEL_SIZE,
        )
    except Exception as e:
        if tc_tif:
            log.warning("GLO-30 download failed (%s), falling back to SNAP TC elevation", e)
            extract_dem_from_snap_tc(tc_tif, dem_tif)
        else:
            raise RuntimeError(f"GLO-30 download failed and no tc_tif fallback: {e}")

    # Run WhiteboxTools pipeline
    hand_path, slope_path = run_dem_pipeline(
        dem_tif, out_dir, stream_threshold=STREAM_THRESHOLD
    )

    # Cleanup extracted DEM (keep only HAND + slope)
    if dem_tif.exists():
        dem_tif.unlink()

    r.set(status_key, "done")
    log.info("=== DEM job done: %s → %s ===", bhash, out_dir)

    _emit_event(r, run_id, "DEM_COMPLETED", {
        "hand_tif": str(hand_path),
        "slope_tif": str(slope_path),
        "basin_hash": bhash,
    })


def _emit_event(r: redis.Redis, run_id: str | None, event: str, artifacts: dict):
    """Push event to control plane events queue.

    Delegates to shared.redis_helpers.emit_event if available.
    """
    if not run_id:
        return
    try:
        from shared.redis_helpers import emit_event
        emit_event(r, run_id, event, artifacts)
    except ImportError:
        r.rpush("cp_events", json.dumps({
            "run_id": run_id,
            "event": event,
            "artifacts": artifacts,
        }))


def main():
    r = connect_redis()
    log.info("DEM Worker ready, listening on 'dem_jobs' queue "
             "(threshold=%d, pixel=%.0fm, CRS=%s) …",
             STREAM_THRESHOLD, DEM_PIXEL_SIZE, TC_EPSG)

    while True:
        try:
            job = r.blpop("dem_jobs", timeout=0)
            payload = json.loads(job[1])
            process_dem_job(payload, r)
        except Exception:
            log.exception("DEM job failed")
            try:
                bhash = payload.get("_hash", "unknown")
                r.set(f"dem:{bhash}:status", "error")
                _emit_event(r, payload.get("run_id"), "FAILED", {
                    "error": "DEM pipeline failed"
                })
            except Exception:
                pass


if __name__ == "__main__":
    main()
