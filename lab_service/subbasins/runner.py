"""The child process of a subbasin_delineation job (spawned by worker.execute with target=subbasins_child,
address-space limited)."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import logging
import resource
import traceback
from datetime import datetime, timezone
from pathlib import Path

from lab_service import settings

log = logging.getLogger("lab")
TABLE_COLS = ("id", "name", "post_id", "down", "strahler", "depth", "local_km2", "total_km2", "z_mean_m", "z_outlet_m",
              "z_max_m", "slope_deg", "link_km", "link_slope")


def config_of(p: dict, entry: dict) -> dict:
    """What the result depends on; its sha256 lets the platform reuse a finished job for the same request."""
    rivers = p.get("rivers") or {}
    rhash = hashlib.sha256(json.dumps(rivers, sort_keys=True, separators=(",", ":")).encode()).hexdigest() if rivers else None
    return {"pipeline": settings.SUBBASIN_PIPELINE_VERSION, "dem": p["dem"], "region": p["region"],
            "outlet": [round(p["outlet_lon"], 6), round(p["outlet_lat"], 6)],
            "stream_threshold_km2": p["stream_threshold_km2"], "burn_depth_m": p["burn_depth_m"],
            "snap_m": settings.SNAP_DISTANCE_M, "window_buffer_m": settings.SUBBASIN_WINDOW_BUFFER_M,
            "gauges": sorted((g["post_id"], g["lon"], g["lat"], g.get("area_km2")) for g in p.get("gauges") or []),
            "rivers_sha256": rhash, "terrain": {k: entry.get(k) for k in ("sha256", "version", "generated_at") if k in entry}}


def table_csv(subbasins: dict) -> bytes:
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(TABLE_COLS + ("up", "outlet_lon", "outlet_lat"))
    for f in subbasins["features"]:
        pr = f["properties"]
        wr.writerow([pr.get(c) for c in TABLE_COLS] + [" ".join(map(str, pr["up"])), *pr["outlet"]])
    return buf.getvalue().encode("utf-8")


def products_of(p: dict, run: dict, cfg_hash: str) -> list[dict]:
    label = settings.DEM_LABELS.get(p["dem"], {"label": p["dem"], "version": p["dem"], "attribution": "", "licence": ""})
    thr = f"{p['stream_threshold_km2']:g} km²"
    common = {"dem": p["dem"], "processing_version": settings.SUBBASIN_PIPELINE_VERSION, "crs": "EPSG:4326",
              "resolution_m": None,
              "source_attribution": f"GeoHydroAI Flood Lab subbasins {settings.SUBBASIN_PIPELINE_VERSION} (QSWAT+ method) — "
                                    f"{label['version']}; {label['attribution']}; river layer: GeoHydroAI Hydro river system",
              "license": f"derived from {label['label']}: {label['licence']}"}
    items = [("subbasins", "Subbasins", "subbasins.geojson", "application/geo+json",
              json.dumps(run["subbasins"], ensure_ascii=False, separators=(",", ":")).encode()),
             ("subbasin_streams", "Stream links", "streams.geojson", "application/geo+json",
              json.dumps(run["streams"], ensure_ascii=False, separators=(",", ":")).encode()),
             ("subbasin_table", "Subbasin table", "subbasins.csv", "text/csv", table_csv(run["subbasins"]))]
    out = []
    for kind, name, fname, media, data in items:
        if len(data) > settings.HAND_PRODUCT_MAX_MB * 2 ** 20:
            from lab_service.pipeline import LabError
            raise LabError(f"{fname} is {len(data) / 2 ** 20:.1f} MB, above the {settings.HAND_PRODUCT_MAX_MB} MB inline limit: "
                           "raise the stream threshold")
        out.append({**common, "kind": kind, "name": f"{name} · {label['label']} · {thr}", "file_name": fname,
                    "media_type": media, "stats": {"bytes": len(data), "config_hash": cfg_hash,
                                                   "subbasins": run["summary"]["subbasins"]},
                    "content_b64": base64.b64encode(data).decode("ascii")})
    return out


def subbasins_child(job: dict, out_dir: str, conn) -> None:
    try:
        resource.setrlimit(resource.RLIMIT_AS, (settings.CHILD_MEM_BYTES, settings.CHILD_MEM_BYTES))
    except (ValueError, OSError):
        pass
    import whitebox

    from lab_service import pipeline as P
    from lab_service.subbasins import pipeline as SP
    from lab_service.terrain import TerrainStore
    out = Path(out_dir)
    try:
        p = job["params"]
        terrain = TerrainStore()
        wbt = whitebox.WhiteboxTools()
        wbt.set_verbose_mode(False)

        def progress(stage: str) -> None:
            conn.send(("progress", SP.STAGES.get(stage, 50), stage))

        progress("terrain")
        paths = terrain.paths(p["dem"], p["district_slug"], ("d8", "streams", "dem_f32"))
        entry = terrain.entry(p["dem"], p["district_slug"])
        run = SP.run_subbasins(wbt, paths, p, out / "_tmp", progress)
        progress("packaging")
        cfg = config_of(p, entry)
        cfg_hash = hashlib.sha256(json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        meta = {"pipeline": settings.SUBBASIN_PIPELINE_VERSION, "method": "QSWAT+ (stream burn-in, a subbasin per link, "
                "gauges as outlets)", "image": settings.LAB_IMAGE_TAG or None, "job_id": job["job_id"],
                "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "config": cfg,
                "dem": settings.DEM_LABELS.get(p["dem"]), "timings_s": run["timings"]}
        table = [{k: f["properties"].get(k) for k in TABLE_COLS + ("up", "outlet")} for f in run["subbasins"]["features"]]
        stats = {**run["summary"], "audit": run["audit"], "gauges": run["gauges"], "table": table,
                 "region": p["region"], "district_name_ua": p.get("district_name_ua"),
                 "quality": {"warnings": run["warnings"]}}
        products = products_of(p, run, cfg_hash)
        result = {"kind": "subbasin_delineation", "stats": stats, "metadata": meta, "config_hash": cfg_hash,
                  "snapped_outlet": run["snapped_outlet"]}
        (out / "result.json").write_text(json.dumps(result, ensure_ascii=False))
        P.cleanup(out / "_tmp")
        conn.send(("done", {"result": result, "products": products}))
    except P.LabError as e:
        conn.send(("failed", str(e)))
    except MemoryError:
        conn.send(("failed", "out of memory: the basin is too large for subbasins on this worker (choose an outlet upstream)"))
    except Exception as e:  # noqa: BLE001
        log.error("job %s crashed:\n%s", job["job_id"], traceback.format_exc())
        conn.send(("failed", f"{type(e).__name__}: {e}"))
    finally:
        conn.close()
