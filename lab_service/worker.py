"""The Flood Lab worker: one job at a time from the Redis list, both DEMs, comparison, callback to Django.
The pipeline runs in a child process (spawn) with an address-space limit, so an out-of-memory basin fails the job
instead of the worker."""
from __future__ import annotations

import json
import logging
import multiprocessing as mp
import resource
import time
import traceback
from pathlib import Path

import redis

from lab_service import settings
from lab_service.callbacks import post_callback
from lab_service.store import JobStore, public_view
from lab_service.terrain import TerrainStore

log = logging.getLogger("lab")
STAGES = {"terrain": 10, "fab_dem": 40, "copernicus_dem": 70, "compare": 90}


def _child(job: dict, out_dir: str, conn) -> None:
    """Runs in the child process: downloads what is missing, delineates on every DEM, compares, writes result.json."""
    try:
        resource.setrlimit(resource.RLIMIT_AS, (settings.CHILD_MEM_BYTES, settings.CHILD_MEM_BYTES))
    except (ValueError, OSError):
        pass
    import whitebox

    from lab_service import pipeline as P
    try:
        p = job["params"]
        terrain = TerrainStore()
        wbt = whitebox.WhiteboxTools()
        wbt.set_verbose_mode(False)
        conn.send(("progress", STAGES["terrain"], "preparing terrain"))
        paths = {dem: terrain.paths(dem, p["district_slug"]) for dem in settings.DEMS}
        runs = {}
        for dem in settings.DEMS:
            conn.send(("progress", STAGES.get(dem, 50), f"watershed {dem}"))
            runs[dem] = P.run_one(wbt, dem, paths[dem], p["outlet_lon"], p["outlet_lat"], Path(out_dir) / "_tmp" / dem)
        conn.send(("progress", STAGES["compare"], "comparison"))
        primary = p.get("primary_dem", settings.PRIMARY_DEM)
        others = [d for d in settings.DEMS if d != primary]
        a = runs[primary]
        by_dem = {d: runs[d]["stats"] for d in settings.DEMS}
        comparison = P.compare(a, runs[others[0]]) if others else None
        for d, r in runs.items():
            (Path(out_dir) / f"basin_{d}.geojson").write_text(json.dumps({"type": "Feature", "properties": {"dem": d, **r["stats"]}, "geometry": r["basin"]}, ensure_ascii=False))
        result = {"basin": a["basin"], "snapped_outlet": a["snapped_outlet"],
                  "stats": {"method": settings.METHOD, "region": p["region"], "district_slug": p["district_slug"], "district_name_ua": p.get("district_name_ua"),
                            "primary_dem": primary, "stream_threshold_cells": settings.STREAM_THRESHOLD_CELLS,
                            "stream_threshold": p.get("stream_threshold"), "snap_distance_m": a["stats"]["snap_distance_m"],
                            "levels_preview": p.get("hand_threshold"), **{k: a["stats"][k] for k in ("area_km2", "relief_m", "main_channel_km") if k in a["stats"]},
                            "by_dem": by_dem, "comparison": comparison}}
        (Path(out_dir) / "result.json").write_text(json.dumps(result, ensure_ascii=False))
        P.cleanup(Path(out_dir) / "_tmp")
        conn.send(("done", result))
    except P.LabError as e:
        conn.send(("failed", str(e)))
    except MemoryError:
        conn.send(("failed", "out of memory: the basin is too large for the Lab"))
    except Exception as e:  # noqa: BLE001
        log.error("job %s crashed:\n%s", job["job_id"], traceback.format_exc())
        conn.send(("failed", f"{type(e).__name__}: {e}"))
    finally:
        conn.close()


def run_job(store: JobStore, job_id: str) -> None:
    rec = store.load(job_id)
    if rec is None or rec["status"] != "queued":
        return
    p = rec["params"]
    cb = lambda body: post_callback(p.get("callback_url"), p.get("callback_token"), {"job_id": job_id, **body})
    store.update(job_id, status="running", progress_pct=5, stage="starting")
    cb({"status": "running", "progress_pct": 5, "error": None})
    parent, child = mp.Pipe(duplex=False)
    proc = mp.get_context("spawn").Process(target=_child, args=(rec, str(store.out_dir(job_id)), child), daemon=False)
    proc.start()
    child.close()
    outcome = None
    while True:
        try:
            kind, *rest = parent.recv()
        except EOFError:
            break
        if kind == "progress":
            pct, stage = rest
            store.update(job_id, progress_pct=pct, stage=stage)
            cb({"status": "running", "progress_pct": pct, "error": None})
        else:
            outcome = (kind, rest[0])
    proc.join()
    if outcome is None:
        outcome = ("failed", "out of memory (worker killed)" if proc.exitcode == -9 else f"worker exited with code {proc.exitcode}")
    kind, payload = outcome
    if kind == "done":
        rec = store.update(job_id, status="done", progress_pct=100, stage="done", result=payload, error=None)
    else:
        rec = store.update(job_id, status="failed", progress_pct=100, stage="failed", error=payload)
    cb({k: v for k, v in public_view(rec).items() if k != "job_id"})
    log.info("job %s %s", job_id, rec["status"])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = JobStore()
    r = redis.Redis.from_url(settings.PLATFORM_REDIS_URL)
    log.info("Flood Lab worker: queue %s, terrain %s", settings.QUEUE_KEY, settings.TERRAIN_LOCAL or f"s3://{settings.S3_BUCKET}/{settings.TERRAIN_PREFIX}")
    last_sweep = 0.0
    while True:
        item = r.brpop(settings.QUEUE_KEY, timeout=30)
        if item:
            job_id = item[1].decode()
            try:
                run_job(store, job_id)
            except Exception:  # noqa: BLE001
                log.error("job %s: unexpected error\n%s", job_id, traceback.format_exc())
        if time.time() - last_sweep > 3600:
            store.sweep()
            last_sweep = time.time()


if __name__ == "__main__":
    main()
