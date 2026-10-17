"""
Transboundary river-basin districts: each of the 16 Ukrainian districts plus the foreign land that drains INTO it.

The district polygons of data/ukraine_basins.geojson stop at the state border, so the terrain cut to them (national_hand.py)
lost every upstream catchment abroad: a basin of a river that enters Ukraine (Pripyat, Desna, Upper Dnipro, Siversky
Donets, Siret, Tisza tributaries…) was clipped at the border in the Flood Lab. Here the border is only a layer.

Method (90 m D8 of national_90m.py, EPSG:3035):
  ua      = the districts rasterised (1..16)
  entries = Ukrainian cells that receive D8 flow from a foreign cell; each gets its own id
  WhiteboxTools Watershed(d8, entries) → every foreign cell labelled with the first Ukrainian cell its flow reaches
  A foreign catchment that touches the edge of the DEM mosaic is incomplete (Danube above the delta, cut at 21° E) and
  is dropped, as are cells that never reach Ukraine. Each district = its Ukrainian polygon ∪ its foreign catchments
  (the Ukrainian parts are unchanged, districts still do not overlap).

  docker run --rm -v ./data:/data -v ./scripts:/scripts projects-dem_worker \
    python3 /scripts/national/transboundary_districts.py [--dem copernicus_dem]
Outputs data/national/_districts_tb/{<slug>.geojson, <slug>_buf.geojson, index.json, report.json}: same format as
data/national/_districts (EPSG:3035 cut lines, 3 km buffer), so national_hand.py / stage_dem_f32.py take it with --cuts.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio import features
from scipy import ndimage
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

D8 = {1: (-1, 1), 2: (0, 1), 4: (1, 1), 8: (1, 0), 16: (1, -1), 32: (0, -1), 64: (-1, -1), 128: (-1, 0)}
CRS = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3035"}}
BUFFER_M = 3000.0


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def shifted(a: np.ndarray, dr: int, dc: int) -> np.ndarray:
    """out[r+dr, c+dc] = a[r, c] (zeros where nothing shifts in)."""
    out = np.zeros_like(a)
    h, w = a.shape
    out[max(dr, 0):h + min(dr, 0), max(dc, 0):w + min(dc, 0)] = a[max(-dr, 0):h - max(dr, 0), max(-dc, 0):w - max(dc, 0)]
    return out


def fc(name: str, code: str, geom) -> dict:
    return {"type": "FeatureCollection", "name": name, "crs": CRS,
            "features": [{"type": "Feature", "properties": {"code": code}, "geometry": mapping(geom)}]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", default="copernicus_dem")
    ap.add_argument("--data", default="/data")
    ap.add_argument("--min-km2", type=float, default=1.0, help="drop foreign pieces smaller than this")
    a = ap.parse_args()
    data = Path(a.data)
    src_dir, out = data / "national" / "_districts", data / "national" / "_districts_tb"
    work = data / "national" / a.dem / "90m"
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    index = json.loads((src_dir / "index.json").read_text())
    geoms = {d["slug"]: unary_union([shape(f["geometry"]) for f in json.loads((src_dir / f"{d['slug']}.geojson").read_text())["features"]])
             for d in index}

    with rasterio.open(work / "d8.tif") as ds:
        prof, transform, shp = ds.profile, ds.transform, ds.shape
        d8 = ds.read(1)
    with rasterio.open(work / "dem.tif") as ds:
        valid = ds.read_masks(1) > 0
    cell_km2 = abs(transform.a * transform.e) / 1e6
    ua = features.rasterize([(g, i + 1) for i, g in enumerate(geoms.values())], out_shape=shp, transform=transform, dtype="uint8")
    log(f"grid {shp}, Ukrainian cells {int((ua > 0).sum())}")

    foreign = (ua == 0) & valid
    entry = np.zeros(shp, bool)
    for code, (dr, dc) in D8.items():
        entry |= shifted(foreign & (d8 == code), dr, dc)
    entry &= ua > 0
    ids = np.zeros(shp, "int32")
    n = int(entry.sum())
    ids[entry] = np.arange(1, n + 1, dtype="int32")
    entry_district = np.concatenate([[0], ua[entry]]).astype("uint8")
    log(f"entry cells {n}")
    del entry

    pour = out / "tb_entries.tif"
    p = {**prof, "dtype": "int32", "nodata": 0, "compress": "deflate", "tiled": True, "blockxsize": 512, "blockysize": 512, "BIGTIFF": "YES"}
    with rasterio.open(pour, "w", **p) as ds:
        ds.write(ids, 1)
    del ids
    from whitebox import WhiteboxTools
    wbt = WhiteboxTools()
    wbt.set_verbose_mode(False)
    ws_path = out / "tb_watershed.tif"
    if wbt.watershed(str(work / "d8.tif"), str(pour), str(ws_path)) != 0:
        raise RuntimeError("WhiteboxTools watershed failed")
    with rasterio.open(ws_path) as ds:
        ws = ds.read(1).astype("int64")
        ws[~np.isfinite(ws) | (ws < 0)] = 0
    ws[~foreign] = 0
    log(f"watershed done, foreign cells reaching Ukraine {int((ws > 0).sum())}")

    # incomplete catchments: any labelled cell next to the edge of the DEM mosaic or of the grid
    edge = ndimage.binary_dilation(~valid, iterations=1)
    edge[0, :] = edge[-1, :] = edge[:, 0] = edge[:, -1] = True
    bad = np.unique(ws[edge & (ws > 0)])
    sizes = np.bincount(ws.ravel(), minlength=n + 1)
    dropped_km2 = {}
    for i in bad[bad > 0]:
        s = index[entry_district[i] - 1]["slug"]
        dropped_km2[s] = dropped_km2.get(s, 0.0) + sizes[i] * cell_km2
    keep = (ws > 0) & ~np.isin(ws, bad)
    lab = np.where(keep, entry_district[np.where(keep, ws, 0)], 0).astype("uint8")
    del ws, keep, edge

    report = {"created_at": datetime.now(timezone.utc).isoformat(), "dem_source": a.dem, "grid_res_m": transform.a,
              "entry_cells": n, "districts": []}
    new_index = []
    for k, d in enumerate(index, start=1):
        pieces = [shape(g) for g, v in features.shapes(lab, mask=lab == k, transform=transform) if v == k]
        pieces = [g for g in pieces if g.area / 1e6 >= a.min_km2]
        others = unary_union([g for s, g in geoms.items() if s != d["slug"]])
        abroad = unary_union(pieces).difference(others) if pieces else None
        g = geoms[d["slug"]] if abroad is None or abroad.is_empty else unary_union([geoms[d["slug"]], abroad.buffer(0)])
        g = g.buffer(0)
        foreign_km2 = 0.0 if abroad is None else abroad.area / 1e6
        (out / f"{d['slug']}.geojson").write_text(json.dumps(fc(d["slug"], d["code"], g)))
        (out / f"{d['slug']}_buf.geojson").write_text(json.dumps(fc(f"{d['slug']}_buf", d["code"], g.buffer(BUFFER_M).simplify(30))))
        new_index.append({**d, "km2": round(g.area / 1e6), "km2_ukraine": d["km2"], "km2_abroad": round(foreign_km2)})
        report["districts"].append({"slug": d["slug"], "name_ua": d["name_ua"], "km2_ukraine": d["km2"], "km2_abroad": round(foreign_km2),
                                    "km2_dropped_incomplete": round(dropped_km2.get(d["slug"], 0.0))})
        log(f"{d['slug']:6s} {d['name_ua'][:40]:40s} UA {d['km2']:>7} + abroad {foreign_km2:>9.0f}  dropped {dropped_km2.get(d['slug'], 0):>9.0f}")
    (out / "index.json").write_text(json.dumps(new_index, ensure_ascii=False, indent=1))
    report["seconds"] = round(time.time() - t0, 1)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    log(f"done in {report['seconds']} s → {out}")


if __name__ == "__main__":
    main()
