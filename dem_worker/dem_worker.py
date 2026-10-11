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
PRODUCTS_DIR = DATA_DIR / "products"
# Regional DEM extent used when the contract does not pass dem_bbox: Cheremosh / upper Prut
# (frontend AOI 24.70–25.14 E, 47.69–48.13 N) with margin so basins are not truncated.
DEFAULT_DEM_BBOX = [float(v) for v in os.environ.get(
    "DEFAULT_DEM_BBOX", "24.40,47.55,25.45,48.35").split(",")]


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
    payload["_hash"] = bhash
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


def process_basin_job(payload: dict, r: redis.Redis):
    """basin.delineation.v1: terrain (cached) → watershed → passport → COGs → Terracotta → S3."""
    from whitebox import WhiteboxTools

    from basin_delineation import DelineationError, delineate
    from terrain import ensure_terrain
    import s3_upload
    import tc_ingest

    basin_id = payload["basin_id"]
    run_id = payload.get("run_id")
    progress_key = f"basin:{basin_id}:progress"

    def progress(pct: int, stage: str):
        r.set(progress_key, json.dumps({"pct": pct, "stage": stage}))

    wbt = WhiteboxTools()
    wbt.set_verbose_mode(False)

    try:
        bbox = payload.get("dem_bbox") or DEFAULT_DEM_BBOX
        terrain = ensure_terrain(wbt, payload["dem_source"], bbox, payload["target_epsg"],
                                 float(payload["pixel_size_m"]), progress=progress)
        out_dir = PRODUCTS_DIR / basin_id
        manifest = delineate(wbt, terrain, payload, out_dir, progress=progress)

        progress(98, "registering tiles")
        n_tc = tc_ingest.ingest_manifest(manifest, out_dir)

        s3 = None
        if s3_upload.enabled():
            progress(99, "uploading to S3")
            s3 = s3_upload.upload_basin(payload["dem_source"], basin_id, out_dir)
            manifest["storage"] = {"s3_root": s3["root"], "objects": s3["objects"]}
            (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
            # re-upload the manifest so the S3 copy also carries the storage block
            s3_upload.upload_manifest(payload["dem_source"], basin_id, out_dir)

        progress(100, "done")
        _emit_event(r, run_id, "BASIN_COMPLETED", {
            "basin_id": basin_id,
            "manifest": str(out_dir / "manifest.json"),
            "area_km2": manifest["stats"]["area_km2"],
            "tc_datasets": n_tc,
            **({"s3_root": s3["root"]} if s3 else {}),
        })
    except DelineationError as e:
        log.warning("Basin %s rejected: %s", basin_id, e)
        progress(100, "failed")
        _emit_event(r, run_id, "FAILED", {"basin_id": basin_id, "error": str(e)})
    except Exception as e:
        log.exception("Basin %s failed", basin_id)
        progress(100, "failed")
        _emit_event(r, run_id, "FAILED", {"basin_id": basin_id, "error": f"{type(e).__name__}: {e}"})


def _basin_child(payload: dict):
    process_basin_job(payload, connect_redis())


def _run_isolated(payload: dict):
    """Run one basin job in a child process: numpy/GDAL memory is returned to the OS when it exits
    (the long-lived worker otherwise keeps ~0.5 GB after a large basin). A hard crash or OOM kill
    of the child is reported as FAILED instead of taking the worker down."""
    import multiprocessing as mp
    proc = mp.get_context("spawn").Process(target=_basin_child, args=(payload,), daemon=False)
    proc.start()
    proc.join()
    if proc.exitcode != 0:
        log.error("Basin job %s: child exited with code %s", payload.get("basin_id"), proc.exitcode)
        r = connect_redis()
        r.set(f"basin:{payload.get('basin_id')}:progress", json.dumps({"pct": 100, "stage": "failed"}))
        reason = "out of memory (killed)" if proc.exitcode == -9 else f"worker process exited with code {proc.exitcode}"
        _emit_event(r, payload.get("run_id"), "FAILED", {"basin_id": payload.get("basin_id"), "error": reason})


def main():
    import s3_upload
    s3_upload.check_config()
    r = connect_redis()
    log.info("DEM Worker ready, listening on 'basin_jobs' + 'dem_jobs' "
             "(threshold=%d, pixel=%.0fm, CRS=%s) …",
             STREAM_THRESHOLD, DEM_PIXEL_SIZE, TC_EPSG)

    while True:
        payload: dict = {}
        try:
            queue, raw = r.blpop(["basin_jobs", "dem_jobs"], timeout=0)
            payload = json.loads(raw)
            if queue == "basin_jobs":
                _run_isolated(payload)
            else:
                process_dem_job(payload, r)
        except Exception:
            log.exception("DEM job failed")
            try:
                if payload.get("_hash"):
                    r.set(f"dem:{payload['_hash']}:status", "error")
                _emit_event(r, payload.get("run_id"), "FAILED", {
                    "error": "DEM pipeline failed"
                })
            except Exception:
                pass


if __name__ == "__main__":
    main()
