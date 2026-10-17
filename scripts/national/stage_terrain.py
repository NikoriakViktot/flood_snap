"""
Stage the national terrain set for the Flood Lab in the data-lake layout and write its manifest.

  data/s3_staging/hydro/lake/national_terrain/v1/
    _districts/index.json, <slug>.geojson         the 16 river-basin districts (EPSG:4326 cut lines)
    dem_source=<dem>/manifest.json                 per district: files, encodings, sha256, bytes, bbox (EPSG:3035)
    dem_source=<dem>/<slug>/{d8,streams,hand,dem}.tif   hard links of data/national/<dem>/hand_2000/<slug>/
    dem_source=<dem>/<slug>/dem_f32.tif                 optional: Float32 DEM for HAND models (stage_dem_f32.py)

The server never computes terrain: lab_service reads these files from S3 (read-through cache). Upload with
  python3 scripts/national/upload_lake.py --staging /data/s3_staging --prefix hydro/lake/national_terrain

  ~/repo/data_dnipro_h_q/.venv-linux/bin/python scripts/national/stage_terrain.py --dem fab_dem
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
FILES = ("d8.tif", "streams.tif", "dem.tif")
# staged where present (stage_dem_f32.py); the manifest lists them per district, lab_service checks `has()`.
# hand.tif (national HAND) is only in v1: from v2 HAND is built per basin in the Flood Lab, validated and published there.
OPTIONAL_FILES = ("hand.tif", "dem_f32.tif")
ENCODING = {
    "d8.tif": {"dtype": "uint8", "encoding": "wbt_d8_pointer", "nodata": 0, "note": "WhiteboxTools D8 pointer codes 1,2,4,…,128"},
    "streams.tif": {"dtype": "uint8", "nodata": 0, "note": "1 = stream cell, flow accumulation >= 2000 cells"},
    "hand.tif": {"dtype": "float64", "units": "m", "nodata": -9999, "note": "height above nearest drainage (stream_threshold_cells)"},
    "dem.tif": {"dtype": "int16", "units": "m", "nodata": -32768, "note": "warped DEM, bilinear, rounded to metres"},
    "dem_f32.tif": {"dtype": "float32", "units": "m", "nodata": -9999, "vertical_datum": "EGM2008",
                    "note": "warped DEM, bilinear, unrounded, buffered district (3 km) — Flood Lab HAND models"},
}


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def link(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if dst.stat().st_size == src.stat().st_size:
            return
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def main():
    import rasterio

    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", default="fab_dem")
    ap.add_argument("--threshold", type=int, default=2000)
    ap.add_argument("--cuts", default="_districts", help="district cut lines (_districts_tb: with the foreign catchments)")
    ap.add_argument("--suffix", default="", help="source hand_<threshold><suffix>")
    ap.add_argument("--version", default="v1", help="national_terrain/<version> (v2: transboundary districts)")
    a = ap.parse_args()
    src_root = DATA / "national" / a.dem / f"hand_{a.threshold}{a.suffix}"
    cuts = DATA / "national" / a.cuts
    stage = DATA / "s3_staging" / "hydro" / "lake" / "national_terrain" / a.version
    (stage / "_districts").mkdir(parents=True, exist_ok=True)
    index = json.loads((cuts / "index.json").read_text())
    shutil.copy2(cuts / "index.json", stage / "_districts" / "index.json")
    for d in index:
        shutil.copy2(cuts / f"{d['slug']}.geojson", stage / "_districts" / f"{d['slug']}.geojson")
    districts = []
    for d in index:
        src = src_root / d["slug"]
        missing = [f for f in FILES if not (src / f).exists()]
        if missing:
            raise SystemExit(f"{d['slug']}: missing {missing}; run national_hand.py --terrain-only first")
        files = {}
        with rasterio.open(src / "d8.tif") as r:
            bbox = [round(v, 1) for v in r.bounds]
            shape = [r.height, r.width]
            transform = list(r.transform)[:6]
        for f in FILES + tuple(f for f in OPTIONAL_FILES if (src / f).exists()):
            p = src / f
            link(p, stage / f"dem_source={a.dem}" / d["slug"] / f)
            files[f] = {**ENCODING[f], "bytes": p.stat().st_size, "sha256": sha256(p)}
        run = json.loads((src / "run.json").read_text()) if (src / "run.json").exists() else {}
        districts.append({"code": d["code"], "slug": d["slug"], "name_ua": d["name_ua"], "km2": d["km2"],
                          "km2_abroad": d.get("km2_abroad", 0), "bbox_3035": bbox,
                          "shape": shape, "transform": transform, "files": files, "computed_at": run.get("created_at")})
        print(d["slug"], {f: round(files[f]["bytes"] / 1e6, 1) for f in files}, "MB", flush=True)
    manifest = {"schema": "national_terrain.v1", "terrain_version": a.version, "districts_cut": a.cuts, "dem_source": a.dem, "crs": "EPSG:3035", "res_m": 30,
                "stream_threshold_cells": a.threshold, "buffer_m": 3000,
                "conditioning": "breach_depressions(fill_pits=True, flat_increment=0.001)", "flow_routing": "D8",
                "optional_files": list(OPTIONAL_FILES),
                "built_by": "flood_snap scripts/national/national_hand.py (+ --terrain-only), stage_terrain.py",
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "districts": districts}
    (stage / f"dem_source={a.dem}" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    total = sum(f["bytes"] for d in districts for f in d["files"].values())
    print(f"{a.dem}: {len(districts)} districts, {total / 2**30:.2f} GiB → {stage / f'dem_source={a.dem}'}")


if __name__ == "__main__":
    main()
