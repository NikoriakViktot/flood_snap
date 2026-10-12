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
    python3 /scripts/national/national_hand.py --dem fab_dem [--only 5_3_3]
Outputs: data/national/<dem>/hand_2000/<slug>/{hand.tif, streams.tif, run.json}
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
    steps = [("breach_depressions", lambda: w.breach_depressions(str(dem), str(br), fill_pits=True, flat_increment=0.001)),
             ("d8_pointer", lambda: w.d8_pointer(str(br), str(d8))),
             ("d8_flow_accumulation", lambda: w.d8_flow_accumulation(str(br), str(acc), out_type="cells")),
             ("extract_streams", lambda: w.extract_streams(str(acc), str(st), threshold=a.threshold, zero_background=False)),
             ("elevation_above_stream", lambda: w.elevation_above_stream(str(br), str(st), str(hand)))]
    times = {}
    for name, fn in steps:
        s = time.time()
        if fn() != 0:
            raise RuntimeError(f"{d['code']}: WhiteboxTools {name} failed")
        times[name] = round(time.time() - s, 1)
    for src, name, nodata in ((hand, "hand.tif", "-9999"), (st, "streams.tif", "0")):
        tmp = out / f"{name}.tmp.tif"
        sh("gdalwarp", "-q", "-overwrite", "-cutline", str(cuts / f"{d['slug']}.geojson"), "-dstnodata", nodata, *CO, str(src), str(tmp))
        tmp.replace(out / name)
    run = {"district": d["code"], "name_ua": d["name_ua"], "km2": d["km2"], "dem": a.dem, "threshold_cells": a.threshold,
           "crs": EPSG, "res_m": RES, "buffer_m": 3000, "seconds": round(time.time() - t0, 1), "steps_s": times,
           "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (out / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1))
    shutil.rmtree(work, ignore_errors=True)
    return run


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", choices=list(VRT), required=True)
    ap.add_argument("--threshold", type=int, default=2000)
    ap.add_argument("--root", default="/data/national")
    ap.add_argument("--only", default="", help="comma list of district slugs (e.g. 5_3_3,M5_2)")
    a = ap.parse_args()
    cuts = Path(a.root) / "_districts"
    only = {c.strip() for c in a.only.split(",") if c.strip()}
    base = Path(a.root) / a.dem / f"hand_{a.threshold}"
    for d in json.loads((cuts / "index.json").read_text()):                       # smallest first
        if only and d["slug"] not in only:
            continue
        out = base / d["slug"]
        if (out / "hand.tif").exists():
            print(f"skip {d['code']} (done)", flush=True)
            continue
        print(f"{d['code']} {d['name_ua']} {d['km2']:,} km² …", flush=True)
        run = district(a, d, cuts, Path(a.root) / a.dem / "_hand_work" / d["slug"], out)
        print(f"  done in {run['seconds']:.0f} s {run['steps_s']}", flush=True)


if __name__ == "__main__":
    main()
