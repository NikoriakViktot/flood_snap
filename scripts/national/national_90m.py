"""
National pass at 90 m (EPSG:3035, equal area): one D8 terrain for Ukraine + transboundary upstream,
every hydro-ua gauge snapped (area-guided when the official catchment area is known), one
WhiteboxTools watershed run for all outlets → incremental catchments → nested total basins.

Run inside the dem_worker image:
  docker run --rm -v ./data:/data -v ./scripts:/scripts -v ./dem_worker:/app \
    projects-dem_worker python3 /scripts/national/national_90m.py --dem copernicus_dem

Outputs (data/national/<dem>/90m/):
  dem.tif breached.tif d8.tif acc.tif      terrain (kept for reuse)
  pour.tif watershed.tif                   outlets + incremental catchments
  gauges_90m.csv                           per gauge: snap, areas, parent, tier, flags
  basins_90m.geojson                       total basin per gauge (EPSG:4326)
  run.json                                 provenance (timings, WBT version, parameters)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio import features
from rasterio.windows import Window
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform, unary_union

EPSG = "EPSG:3035"
RES = 90.0
SNAP_M = 2000.0          # yearbook coordinates can be several hundred metres off the channel
MIN_CAND_KM2 = 5.0       # candidate cells must drain at least this (no snapping onto gullies)
TIER_30M_MAX_KM2 = 20000 # refine at 30 m up to this area; larger basins stay at 90 m

# WhiteboxTools D8 pointer (non-ESRI): 1 NE, 2 E, 4 SE, 8 S, 16 SW, 32 W, 64 NW, 128 N
D8 = {1: (-1, 1), 2: (0, 1), 4: (1, 1), 8: (1, 0), 16: (1, -1), 32: (0, -1), 64: (-1, -1), 128: (-1, 0)}

VRT = {"copernicus_dem": "/data/dem/glo30/glo30_4326.vrt", "fab_dem": "/data/dem/fabdem/fabdem_4326.vrt"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]}: {p.stderr[-800:]}")


def wbt_tool():
    from whitebox import WhiteboxTools
    w = WhiteboxTools()
    w.set_verbose_mode(False)
    return w


def check(name, ret):
    if ret != 0:
        raise RuntimeError(f"WhiteboxTools {name} failed ({ret})")


def build_terrain(dem_source: str, out: Path, timings: dict):
    wbt = wbt_tool()
    wbt.set_working_dir(str(out))
    dem, br, d8, acc = (out / n for n in ("dem.tif", "breached.tif", "d8.tif", "acc.tif"))
    if not dem.exists():
        t = time.time()
        log("warp VRT → EPSG:3035 90 m (average)")
        run(["gdalwarp", "-q", "-t_srs", EPSG, "-tr", str(RES), str(RES), "-tap", "-r", "average",
             "-srcnodata", "-9999", "-dstnodata", "-9999", "-wm", "1024", "-multi", "-wo", "NUM_THREADS=ALL_CPUS",
             "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", "-co", "BIGTIFF=YES", VRT[dem_source], str(dem)])
        timings["warp_s"] = round(time.time() - t, 1)
    for name, path, fn in (
        ("breach_depressions", br, lambda: wbt.breach_depressions(str(dem), str(br), fill_pits=True)),
        ("d8_pointer", d8, lambda: wbt.d8_pointer(str(br), str(d8))),
        ("d8_flow_accumulation", acc, lambda: wbt.d8_flow_accumulation(str(br), str(acc), out_type="cells")),
    ):
        if not path.exists():
            t = time.time()
            log(name)
            check(name, fn())
            timings[f"{name}_s"] = round(time.time() - t, 1)
    return wbt, dem, br, d8, acc


def load_gauges(path: Path) -> list[dict]:
    rows = json.loads(path.read_text())["data"]
    return [r for r in rows if r.get("lat") and r.get("lon")]


def snap_all(gauges, acc_path: Path):
    """Area-guided snapping on the 90 m grid. Returns list of dicts with row/col and diagnostics."""
    out = []
    with rasterio.open(acc_path) as src:
        tr = Transformer.from_crs("EPSG:4326", EPSG, always_xy=True)
        px_km2 = abs(src.transform.a * src.transform.e) / 1e6
        rc = int(math.ceil(SNAP_M / RES))
        min_cells = MIN_CAND_KM2 / px_km2
        for g in gauges:
            x, y = tr.transform(g["lon"], g["lat"])
            row, col = src.index(x, y)
            rec = {"post_id": g["post_id"], "name": g.get("post_name_en") or g.get("post_name_ua"),
                   "river": g.get("river"), "basin_code": g.get("basin_code"),
                   "lon": g["lon"], "lat": g["lat"], "official_km2": g.get("F_km2")}
            if not (0 <= row < src.height and 0 <= col < src.width):
                out.append({**rec, "status": "outside_dem"})
                continue
            r0, c0 = max(row - rc, 0), max(col - rc, 0)
            win = Window(c0, r0, min(col + rc + 1, src.width) - c0, min(row + rc + 1, src.height) - r0)
            a = src.read(1, window=win, masked=True).filled(0).astype(np.float64)
            rr, cc = np.nonzero(a >= min_cells)
            if rr.size == 0:
                out.append({**rec, "status": "no_channel_within_snap"})
                continue
            d = np.hypot(rr + r0 - row, cc + c0 - col) * RES
            keep = d <= SNAP_M
            rr, cc, d = rr[keep], cc[keep], d[keep]
            if rr.size == 0:
                out.append({**rec, "status": "no_channel_within_snap"})
                continue
            km2 = a[rr, cc] * px_km2
            F = g.get("F_km2")
            if F:
                score = np.abs(np.log(km2 / F)) + 0.25 * d / SNAP_M
                method = "area_guided"
            else:
                # no official area: nearest cell draining ≥ 25 km² (gauges are on real rivers)
                big = km2 >= 25
                score = np.where(big, d, d + 1e9) if big.any() else d
                method = "nearest_channel_25km2"
            k = int(np.argmin(score))
            out.append({**rec, "status": "ok", "row": int(rr[k] + r0), "col": int(cc[k] + c0),
                        "snap_m": round(float(d[k]), 1), "snap_method": method,
                        "acc_km2_at_snap": round(float(km2[k]), 2)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", default="copernicus_dem", choices=list(VRT))
    ap.add_argument("--gauges", default="/data/gauges/hydro_ua_river_gauges.json")
    ap.add_argument("--out", default="/data/national")
    a = ap.parse_args()

    out = Path(a.out) / a.dem / "90m"
    out.mkdir(parents=True, exist_ok=True)
    timings: dict = {}
    t0 = time.time()
    wbt, dem, br, d8p, accp = build_terrain(a.dem, out, timings)

    gauges = load_gauges(Path(a.gauges))
    log(f"{len(gauges)} gauges with coordinates")
    snaps = snap_all(gauges, accp)
    ok = [s for s in snaps if s["status"] == "ok"]
    log(f"snapped {len(ok)}, failed {len(snaps) - len(ok)}")

    # one id per distinct outlet cell (duplicate posts share a cell)
    with rasterio.open(accp) as src:
        prof = src.profile.copy()
        H, W, transform, crs = src.height, src.width, src.transform, src.crs
    cell_id: dict[tuple[int, int], int] = {}
    for s in ok:
        s["outlet_id"] = cell_id.setdefault((s["row"], s["col"]), len(cell_id) + 1)
    pour = np.zeros((H, W), dtype=np.int32)
    for (r, c), i in cell_id.items():
        pour[r, c] = i
    prof = {"driver": "GTiff", "width": W, "height": H, "count": 1, "dtype": "int32", "crs": crs,
            "transform": transform, "nodata": 0, "compress": "deflate", "tiled": True,
            "blockxsize": 512, "blockysize": 512, "BIGTIFF": "YES"}
    pour_path = out / "pour.tif"
    with rasterio.open(pour_path, "w", **prof) as dst:
        dst.write(pour, 1)
    del pour

    t = time.time()
    log("watershed (all outlets)")
    ws_path = out / "watershed.tif"
    check("watershed", wbt.watershed(str(d8p), str(pour_path), str(ws_path)))
    timings["watershed_s"] = round(time.time() - t, 1)

    with rasterio.open(ws_path) as src:
        ws = src.read(1)
        ws_nodata = src.nodata
    ws = np.where((ws == ws_nodata) | ~np.isfinite(ws) | (ws < 0), 0, ws).astype(np.int32)
    with rasterio.open(d8p) as src:
        d8 = src.read(1)
    d8 = np.where(np.isfinite(d8), d8, 0).astype(np.int32)

    # parent outlet = incremental catchment that contains the cell one D8 step below the outlet
    parent: dict[int, int] = {}
    for (r, c), i in cell_id.items():
        dr, dc = D8.get(int(d8[r, c]), (None, None))
        p = 0
        if dr is not None and 0 <= r + dr < H and 0 <= c + dc < W:
            p = int(ws[r + dr, c + dc])
        parent[i] = p if p != i else 0
    del d8

    px_km2 = abs(transform.a * transform.e) / 1e6
    inc_cells = np.bincount(ws.ravel(), minlength=len(cell_id) + 1)
    children: dict[int, list[int]] = {}
    for i, p in parent.items():
        if p:
            children.setdefault(p, []).append(i)

    def subtree(i):
        stack, seen = [i], []
        while stack:
            n = stack.pop()
            seen.append(n)
            stack.extend(children.get(n, []))
        return seen

    t = time.time()
    log("polygonising incremental catchments")
    inc_polys: dict[int, list] = {}
    for g, v in features.shapes(ws, mask=ws > 0, transform=transform):
        inc_polys.setdefault(int(v), []).append(shape(g))
    del ws
    inc_union = {i: unary_union(ps).simplify(RES / 2, preserve_topology=True) for i, ps in inc_polys.items()}
    timings["polygonise_s"] = round(time.time() - t, 1)

    to_wgs = Transformer.from_crs(EPSG, "EPSG:4326", always_xy=True).transform
    feats, total_by_outlet = [], {}
    for i in cell_id.values():
        ids = subtree(i)
        total_by_outlet[i] = (sum(int(inc_cells[j]) for j in ids) * px_km2, ids)
    for s in snaps:
        if s["status"] != "ok":
            continue
        i = s["outlet_id"]
        area, ids = total_by_outlet[i]
        geom = unary_union([inc_union[j] for j in ids if j in inc_union])
        F = s["official_km2"]
        ratio = area / F if F else None
        s.update({
            "area_90m_km2": round(area, 2),
            "area_ratio": round(ratio, 4) if ratio else None,
            "parent_outlet": parent.get(i) or None,
            "n_upstream_outlets": len(ids) - 1,
            "touches_edge": None,
            "tier": "30m" if (F or area) <= TIER_30M_MAX_KM2 else "90m",
            "flag": ("area_mismatch" if ratio and not 0.8 <= ratio <= 1.25 else
                     "no_official_area" if not F else "ok"),
        })
        minx, miny, maxx, maxy = shp_transform(to_wgs, geom.envelope).bounds
        s["bbox_wgs84"] = [round(minx, 5), round(miny, 5), round(maxx, 5), round(maxy, 5)]
        feats.append({"type": "Feature", "geometry": mapping(shp_transform(to_wgs, geom)),
                      "properties": {k: s[k] for k in ("post_id", "name", "river", "basin_code", "official_km2",
                                                       "area_90m_km2", "area_ratio", "flag", "tier", "snap_m")}})

    (out / "basins_90m.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    cols = ["post_id", "name", "river", "basin_code", "lon", "lat", "status", "official_km2", "area_90m_km2",
            "area_ratio", "flag", "tier", "snap_m", "snap_method", "acc_km2_at_snap", "outlet_id",
            "parent_outlet", "n_upstream_outlets", "bbox_wgs84"]
    with open(out / "gauges_90m.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for s in snaps:
            w.writerow({**s, "bbox_wgs84": json.dumps(s.get("bbox_wgs84")) if s.get("bbox_wgs84") else ""})

    timings["total_s"] = round(time.time() - t0, 1)
    with rasterio.open(dem) as src:
        grid = {"width": src.width, "height": src.height, "crs": src.crs.to_string(), "res_m": RES}
    (out / "run.json").write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(), "dem_source": a.dem, "vrt": VRT[a.dem],
        "grid": grid, "snap_m": SNAP_M, "min_candidate_km2": MIN_CAND_KM2, "tier_30m_max_km2": TIER_30M_MAX_KM2,
        "whitebox": wbt.version().splitlines()[0], "timings": timings,
        "counts": {"gauges": len(snaps), "snapped": len(ok), "outlets": len(cell_id)},
    }, indent=2))
    log("done", json.dumps(timings))


if __name__ == "__main__":
    sys.exit(main())
