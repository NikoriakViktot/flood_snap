"""
30 m basin passports for every gauge from the national 90 m pass (tier == "30m").

For each gauge: window = 90 m basin bbox + buffer, terrain at 30 m in EPSG:3035 from the local VRT,
area-guided outlet snapping (official F_km2, else the 90 m area), stream threshold scaled to the
basin size, full passport (manifest.json + vectors); rasters only for basins ≤ RASTER_MAX_KM2.
The per-gauge terrain is deleted afterwards. Resumable: gauges with a manifest are skipped.

Run inside the dem_worker image:
  docker run --rm -v ./data:/data -v ./scripts:/scripts -v ./dem_worker:/app -w /app \
    -e DEM_VRT_COPERNICUS_DEM=/data/dem/glo30/glo30_4326.vrt \
    projects-dem_worker python3 /scripts/national/gauge_batch.py --dem copernicus_dem --workers 4

Outputs: data/national/<dem>/gauges/post_id_<id>/…, data/national/<dem>/gauges_30m.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

EPSG = "EPSG:3035"
RES = 30.0
BUFFER_DEG = 0.04          # ~3–4 km around the 90 m basin so the 30 m divide is never clipped
SNAP_M = 1000.0
RASTER_MAX_KM2 = 5000.0
LEVELS = list(range(1, 11))


def letters(post_id: int) -> str:
    """Letters-only id (the frontend rewrites tile URLs with replaceAll('2000', …))."""
    return "g" + "".join("abcdefghij"[int(d)] for d in str(post_id))


def stream_threshold(area_km2: float) -> int:
    # ~1/20 of the basin, between 100 and 2000 cells (0.09–1.8 km² at 30 m)
    return int(max(100, min(2000, area_km2 * 1e6 / (RES * RES) / 20)))


def one(row: dict, dem: str, out_root: str) -> dict:
    sys.path.insert(0, "/app")
    import terrain as T
    from basin_delineation import DelineationError, delineate
    from whitebox import WhiteboxTools

    t0 = time.time()
    pid = int(row["post_id"])
    out_dir = Path(out_root) / "gauges" / f"post_id_{pid}"  # no "=": WhiteboxTools splits CLI args on it
    res = {"post_id": pid, "name": row["name"], "river": row["river"], "basin_code": row["basin_code"],
           "official_km2": row["official_km2"] or None, "area_90m_km2": row["area_90m_km2"]}
    if (out_dir / "manifest.json").exists():
        m = json.loads((out_dir / "manifest.json").read_text())
        return {**res, **summary(m), "status": "cached", "seconds": 0}

    # one terrain dir per gauge: gauges on the same river can share a window (same cache key),
    # and a finished worker deleting the shared terrain broke the others mid-run
    T.TERRAIN_DIR = Path(out_root) / "_terrain" / f"post_id_{pid}"
    official = float(row["official_km2"]) if row["official_km2"] else None
    a90 = float(row["area_90m_km2"])
    target = official or a90
    w, s, e, n = json.loads(row["bbox_wgs84"])
    ratio90 = float(row["area_ratio"]) if row["area_ratio"] else None
    if official and ratio90 and not 0.8 <= ratio90 <= 1.25:
        # 90 m routing went to the wrong channel (flat Polissia, canals, rounded coordinates):
        # open a window around the gauge sized from the official area so the right river is inside.
        import math
        half_km = 1.2 * math.sqrt(official)
        dlat = half_km / 111.0
        dlon = half_km / (111.0 * math.cos(math.radians(float(row["lat"]))))
        lon, lat = float(row["lon"]), float(row["lat"])
        w, s, e, n = min(w, lon - dlon), min(s, lat - dlat), max(e, lon + dlon), max(n, lat + dlat)
        res["window"] = "expanded_from_official_area"
    lon, lat = float(row["lon"]), float(row["lat"])  # the gauge itself must be inside the window
    w, s, e, n = min(w, lon), min(s, lat), max(e, lon), max(n, lat)
    bbox = [w - BUFFER_DEG, s - BUFFER_DEG, e + BUFFER_DEG, n + BUFFER_DEG]
    wbt = WhiteboxTools()
    wbt.set_verbose_mode(False)
    terr = None
    try:
        terr = T.ensure_terrain(wbt, dem, bbox, EPSG, RES)
        # When the 90 m pass already matched the official area, start from its snapped outlet: it is
        # on the right channel even where the yearbook coordinates sit > 1 km from it.
        out_lon, out_lat, outlet_source = float(row["lon"]), float(row["lat"]), "gauge_coordinates"
        if row.get("snap_lon") and ratio90 and 0.8 <= ratio90 <= 1.25:
            out_lon, out_lat = float(row["snap_lon"]), float(row["snap_lat"])
            outlet_source = "national_90m_snapped_outlet"
        params = {
            "basin_id": letters(pid), "run_id": f"national-{dem}", "project_id": None,
            "outlet_lon": out_lon, "outlet_lat": out_lat,
            "snap_distance_m": SNAP_M, "stream_threshold": stream_threshold(target),
            "hand_threshold_m": 5.0, "max_slope_deg": 8.0, "flood_levels_m": LEVELS,
            "cn_grid_tif": None, "target_area_km2": target,
            "write_rasters": target <= RASTER_MAX_KM2,
        }
        m = delineate(wbt, terr, params, out_dir)
        m["gauge"] = {"post_id": pid, "name": row["name"], "river": row["river"], "basin_code": row["basin_code"],
                      "official_area_km2": official, "area_90m_km2": a90,
                      "target_area_source": "official" if official else "national_90m",
                      "gauge_coordinates": [float(row["lon"]), float(row["lat"])],
                      "outlet_search_from": outlet_source,
                      "source": "hydro-ua /v1/hydro/stations"}
        (out_dir / "manifest.json").write_text(json.dumps(m, indent=2, ensure_ascii=False))
        return {**res, **summary(m), "status": "ok", "seconds": round(time.time() - t0, 1)}
    except DelineationError as ex:
        return {**res, "status": "rejected", "error": str(ex)[:300], "seconds": round(time.time() - t0, 1)}
    except Exception as ex:
        traceback.print_exc()
        return {**res, "status": "error", "error": f"{type(ex).__name__}: {ex}"[:300],
                "seconds": round(time.time() - t0, 1)}
    finally:
        if terr is not None:
            shutil.rmtree(terr.dir, ignore_errors=True)


def summary(m: dict) -> dict:
    s, o, q = m["stats"], m["outlet"], m["quality"]
    return {
        "area_30m_km2": s["area_km2"],
        "ratio_30m": o.get("area_ratio_to_official"),
        "snap_m": o["actual_snap_distance_m"], "snap_warning": o.get("snap_warning") or "",
        "stream_order": s["stream_order"], "longest_flowpath_km": s["longest_flowpath_km"],
        "relief_m": s["relief_m"], "mean_slope_deg": s["mean_slope_deg"],
        "touches_dem_edge": q["touches_dem_edge"],
        "failed_checks": ",".join(c["name"] for c in q["checks"] if not c["ok"]),
    }


COLS = ["post_id", "name", "river", "basin_code", "status", "window", "official_km2", "area_90m_km2", "area_30m_km2",
        "ratio_30m", "snap_m", "snap_warning", "stream_order", "longest_flowpath_km", "relief_m",
        "mean_slope_deg", "touches_dem_edge", "failed_checks", "error", "seconds"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", default="copernicus_dem")
    ap.add_argument("--root", default="/data/national")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", default="", help="comma list of post_ids (testing)")
    a = ap.parse_args()

    root = Path(a.root) / a.dem
    with open(root / "90m" / "gauges_90m.csv") as f:
        rows = [r for r in csv.DictReader(f) if r["status"] == "ok" and r["tier"] == "30m"]
    if a.only:
        keep = {x.strip() for x in a.only.split(",")}
        rows = [r for r in rows if r["post_id"] in keep]
    # largest first so the long jobs do not end up alone at the tail
    rows.sort(key=lambda r: -float(r["official_km2"] or r["area_90m_km2"]))
    print(f"{len(rows)} gauges at 30 m, workers={a.workers}", flush=True)

    results, t0 = [], time.time()
    with ProcessPoolExecutor(a.workers) as pool:
        futs = {pool.submit(one, r, a.dem, str(root)): r["post_id"] for r in rows}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            results.append(r)
            print(f"[{i}/{len(rows)}] {r['post_id']} {r['status']} "
                  f"F={r.get('official_km2')} A30={r.get('area_30m_km2')} ratio={r.get('ratio_30m')} "
                  f"{r.get('seconds')}s {r.get('error', '')}", flush=True)

    out_csv = root / "gauges_30m.csv"
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(results, key=lambda r: int(r["post_id"])):
            w.writerow(r)
    ok = [r for r in results if r["status"] in ("ok", "cached")]
    within = [r for r in ok if r.get("ratio_30m") and 0.9 <= float(r["ratio_30m"]) <= 1.1]
    print(f"DONE {len(ok)}/{len(results)} ok, {len(within)} within ±10 % of the official area, "
          f"{round(time.time() - t0)} s → {out_csv}", flush=True)


if __name__ == "__main__":
    main()
