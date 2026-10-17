"""
National HAND at 30 m for one DEM, river-basin district by district (16 districts of data/ukraine_basins.geojson).

Per district (EPSG:3035, 30 m, district + 3 km buffer so channels at the edge keep their catchment):
  dem.tif → breach_depressions(fill_pits, flat_increment=0.001) → d8_pointer → d8_flow_accumulation (cells) → extract_streams (≥ THRESHOLD)
  → elevation_above_stream = HAND (m), clipped to the district itself (districts do not overlap in the mosaic).
Same WhiteboxTools steps as dem_worker/terrain.py, plus flat_increment: without it breach_depressions carved the
district DEMs down to −2589 m (Prut) and HAND reached thousands of metres; flat water (rivers, reservoirs) also broke
the D8 flow. Threshold 2000 cells (≈1.8 km² at 30 m) as the existing hand_2000
masks. Resumable: a district that already has hand.tif is skipped.

District cut lines (EPSG:3035, with and without the 3 km buffer) are prepared on the host, because the worker image
has GDAL and WhiteboxTools but no geopandas:  data/national/_districts/{<slug>.geojson, <slug>_buf.geojson, index.json}

  docker run --rm -v ./data:/data -v ./scripts:/scripts projects-dem_worker \
    python3 /scripts/national/national_hand.py --dem fab_dem [--only 5_3_3] [--terrain-only]
Outputs: data/national/<dem>/hand_2000/<slug>/{hand.tif, streams.tif, d8.tif, dem.tif, run.json}
  d8.tif   WhiteboxTools D8 pointer (Byte 1..128, nodata 0) of the breached DEM: the Flood Lab delineates a basin
           from any outlet with it (lab_service), no terrain work on the server.
  dem.tif  the warped DEM as Int16 metres (nodata -32768) for relief / hypsometry of a basin.
--terrain-only: for districts that already have hand.tif, add the missing d8.tif / dem.tif (warp + breach + pointer).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

VRT = {"copernicus_dem": "/data/dem/glo30/glo30_4326_aligned.vrt", "fab_dem": "/data/dem/fabdem/fabdem_4326.vrt"}
EPSG, RES = "EPSG:3035", 30.0
CO = ["-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", "-co", "BIGTIFF=YES"]


def sh(*args: str) -> None:
    subprocess.run(list(args), check=True)


def _clip(a, d: dict, cuts: Path, out: Path, items) -> None:
    """Cut work rasters to the unbuffered district: (src, name, nodata, extra gdalwarp args)."""
    for src, name, nodata, extra in items:
        tmp = out / f"{name}.tmp.tif"
        sh("gdalwarp", "-q", "-overwrite", "-cutline", str(cuts / f"{d['slug']}.geojson"), "-dstnodata", nodata, *extra, *CO, str(src), str(tmp))
        tmp.replace(out / name)


def district(a, d: dict, cuts: Path, work: Path, out: Path) -> dict:
    t0 = time.time()
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    dem, br, d8, acc, st, hand = (work / f"{n}.tif" for n in ("dem", "breached", "d8", "acc", "streams", "hand"))
    sh("gdalwarp", "-q", "-overwrite", "-t_srs", EPSG, "-tr", str(RES), str(RES), "-r", "bilinear", "-tap",
       "-cutline", str(cuts / f"{d['slug']}_buf.geojson"), "-crop_to_cutline", "-srcnodata", "-9999", "-dstnodata", "-9999",
       "-ot", "Float32", "-multi", "-wo", "NUM_THREADS=8", "-wm", "2048", *CO, VRT[a.dem], str(dem))
    import whitebox
    w = whitebox.WhiteboxTools()
    w.set_verbose_mode(False)
    terrain_only = a.terrain_only and (out / "hand.tif").exists()
    steps = [("breach_depressions", lambda: w.breach_depressions(str(dem), str(br), fill_pits=True, flat_increment=0.001)),
             ("d8_pointer", lambda: w.d8_pointer(str(br), str(d8)))]
    if not terrain_only:
        steps += [("d8_flow_accumulation", lambda: w.d8_flow_accumulation(str(br), str(acc), out_type="cells")),
                  ("extract_streams", lambda: w.extract_streams(str(acc), str(st), threshold=a.threshold, zero_background=False)),
                  ] + ([] if a.no_hand else [("elevation_above_stream", lambda: w.elevation_above_stream(str(br), str(st), str(hand)))])
    times = {}
    for name, fn in steps:
        s = time.time()
        if fn() != 0:
            raise RuntimeError(f"{d['code']}: WhiteboxTools {name} failed")
        times[name] = round(time.time() - s, 1)
    items = [(d8, "d8.tif", "0", ["-ot", "Byte", "-r", "near"]),
             (dem, "dem.tif", "-32768", ["-ot", "Int16", "-r", "near", "-srcnodata", "-9999"])]
    if not terrain_only:
        items = ([] if a.no_hand else [(hand, "hand.tif", "-9999", [])]) + [(st, "streams.tif", "0", [])] + items
    _clip(a, d, cuts, out, items)
    prev = json.loads((out / "run.json").read_text()) if terrain_only and (out / "run.json").exists() else {}
    run = {**prev, "district": d["code"], "name_ua": d["name_ua"], "km2": d["km2"], "km2_abroad": d.get("km2_abroad", 0), "dem": a.dem, "threshold_cells": a.threshold,
           "crs": EPSG, "res_m": RES, "buffer_m": 3000, "seconds": round(time.time() - t0, 1),
           "steps_s": {**prev.get("steps_s", {}), **times}, "terrain_version": "v2" if a.cuts != "_districts" else "v1", "cuts": a.cuts,
           "files": {"hand.tif": {"dtype": "float32", "units": "m", "nodata": -9999}, "streams.tif": {"dtype": "uint8", "nodata": 0},
                     "d8.tif": {"dtype": "uint8", "encoding": "wbt_d8_pointer", "nodata": 0},
                     "dem.tif": {"dtype": "int16", "units": "m", "nodata": -32768}},
           "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (out / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1))
    shutil.rmtree(work, ignore_errors=True)
    return run


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", choices=list(VRT), required=True)
    ap.add_argument("--threshold", type=int, default=2000)
    ap.add_argument("--root", default="/data/national")
    ap.add_argument("--cuts", default="_districts", help="district cut lines under --root (_districts_tb: with the foreign catchments)")
    ap.add_argument("--no-hand", action="store_true", help="terrain only (d8, streams, dem): HAND is built per basin in the Flood Lab, not nationally")
    ap.add_argument("--suffix", default="", help="output hand_<threshold><suffix> (e.g. _tb for the transboundary districts)")
    ap.add_argument("--only", default="", help="comma list of district slugs (e.g. 5_3_3,M5_2)")
    ap.add_argument("--terrain-only", action="store_true", help="add d8.tif / dem.tif to districts that already have hand.tif")
    a = ap.parse_args()
    cuts = Path(a.root) / a.cuts
    only = {c.strip() for c in a.only.split(",") if c.strip()}
    base = Path(a.root) / a.dem / f"hand_{a.threshold}{a.suffix}"
    for d in json.loads((cuts / "index.json").read_text()):                       # smallest first
        if only and d["slug"] not in only:
            continue
        out = base / d["slug"]
        done = (out / ("streams.tif" if a.no_hand else "hand.tif")).exists() and (not a.terrain_only or (out / "d8.tif").exists() and (out / "dem.tif").exists())
        if done:
            print(f"skip {d['code']} (done)", flush=True)
            continue
        print(f"{d['code']} {d['name_ua']} {d['km2']:,} km² …", flush=True)
        run = district(a, d, cuts, Path(a.root) / a.dem / f"_hand_work{a.suffix}" / d["slug"], out)
        print(f"  done in {run['seconds']:.0f} s {run['steps_s']}", flush=True)


if __name__ == "__main__":
    main()
