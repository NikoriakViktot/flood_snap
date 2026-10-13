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
        # the national terrain has ONE stream network (STREAM_THRESHOLD_CELLS); say so instead of silently ignoring
        # a different request (code audit 2026-10-13)
        requested = p.get("stream_threshold") or "medium"
        if requested != settings.STREAM_THRESHOLD_PRESET:
            for d in by_dem:
                by_dem[d].setdefault("quality", {"edge_touch": False, "warnings": []})["warnings"].append(
                    f"stream_threshold '{requested}' was not applied: the national terrain has one stream network "
                    f"({settings.STREAM_THRESHOLD_CELLS} cells, '{settings.STREAM_THRESHOLD_PRESET}')")
        for d, r in runs.items():
            (Path(out_dir) / f"basin_{d}.geojson").write_text(json.dumps({"type": "Feature", "properties": {"dem": d, **r["stats"]}, "geometry": r["basin"]}, ensure_ascii=False))
        products = [product_of(d, runs[d], p) for d in settings.DEMS if runs[d].get("raster")]
        result = {"basin": a["basin"], "snapped_outlet": a["snapped_outlet"],
                  "stats": {"method": settings.METHOD, "region": p["region"], "district_slug": p["district_slug"], "district_name_ua": p.get("district_name_ua"),
                            "primary_dem": primary, "stream_threshold_cells": settings.STREAM_THRESHOLD_CELLS,
                            "stream_threshold": p.get("stream_threshold"), "stream_threshold_applied": settings.STREAM_THRESHOLD_PRESET,
                            "snap_distance_m": a["stats"]["snap_distance_m"],
                            "levels_preview": p.get("hand_threshold"), **{k: a["stats"][k] for k in ("area_km2", "relief_m", "main_channel_km") if k in a["stats"]},
                            "by_dem": by_dem, "comparison": comparison}}
        (Path(out_dir) / "result.json").write_text(json.dumps(result, ensure_ascii=False))
        P.cleanup(Path(out_dir) / "_tmp")
        conn.send(("done", {"result": result, "products": products}))
    except P.LabError as e:
        conn.send(("failed", str(e)))
    except MemoryError:
        conn.send(("failed", "out of memory: the basin is too large for the Lab"))
    except Exception as e:  # noqa: BLE001
        log.error("job %s crashed:\n%s", job["job_id"], traceback.format_exc())
        conn.send(("failed", f"{type(e).__name__}: {e}"))
    finally:
        conn.close()


def product_of(dem: str, run: dict, params: dict) -> dict:
    """A basin raster as a platform data product (contracts/job_event.v1.json `products[]`): the file travels in the
    event (base64) and the platform stores it — the Lab keeps nothing the platform depends on."""
    import base64
    r = run["raster"]
    w, s, e, n = r["bounds"]
    return {"kind": "hand_basin", "name": f"HAND · {dem} · basin", "dem": dem, "media_type": "image/tiff",
            "processing_version": settings.METHOD, "crs": "EPSG:3857", "resolution_m": r["ground_res_m"],
            "bbox": {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]},
            "stats": {"dem": dem, "bands": ["hand_dm", "channel"], "hand_unit": "dm", "nodata": 255,
                      "width": r["width"], "height": r["height"], "bounds": r["bounds"]},
            "source_attribution": "GeoHydroAI Flood Lab — national D8/HAND (2000 cells), " + ("FABDEM" if dem == "fab_dem" else "Copernicus GLO-30"),
            "content_b64": base64.b64encode(r["bytes"]).decode("ascii")}


def execute(job: dict, out_dir: str, report) -> dict:
    """Run one job in a memory-limited child process; `report(body)` receives every state change (running with
    progress, then done with the result or failed with the error). Returns the final body. Shared by the Celery
    task (report = an event to the platform) and the legacy loop below (report = job file + HTTP callback)."""
    job_id = job["job_id"]
    report({"status": "running", "progress_pct": 5, "stage": "starting", "error": None})
    parent, child = mp.Pipe(duplex=False)
    proc = mp.get_context("spawn").Process(target=_child, args=(job, out_dir, child), daemon=False)
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
            report({"status": "running", "progress_pct": pct, "stage": stage, "error": None})
        else:
            outcome = (kind, rest[0])
    proc.join()
    if outcome is None:
        outcome = ("failed", "out of memory (worker killed)" if proc.exitcode == -9 else f"worker exited with code {proc.exitcode}")
    kind, payload = outcome
    if kind == "done":
        body = {"status": "done", "progress_pct": 100, "stage": "done", "error": None, "result_ref": f"lab:{job_id}",
                "result": payload["result"], "products": payload["products"]}
    else:
        body = {"status": "failed", "progress_pct": 100, "stage": "failed", "error": payload, "result_ref": None,
                "result": None, "products": []}
    report(body)
    log.info("job %s %s", job_id, body["status"])
    return body


def run_job(store: JobStore, job_id: str) -> None:
    """LEGACY transport: job file + HTTP callback (lab_service/api.py)."""
    rec = store.load(job_id)
    if rec is None or rec["status"] != "queued":
        return
    p = rec["params"]

    def report(body: dict) -> None:
        fields = {k: body[k] for k in ("status", "progress_pct", "stage", "error") if k in body}
        if body["status"] == "done":
            fields["result"] = body["result"]
        r = store.update(job_id, **fields)
        if body["status"] in ("done", "failed"):
            body = {k: v for k, v in public_view(r).items() if k != "job_id"}
        post_callback(p.get("callback_url"), p.get("callback_token"), {"job_id": job_id, **body})

    execute(rec, str(store.out_dir(job_id)), report)


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
