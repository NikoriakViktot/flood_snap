"""
Stage the Float32 DEM the Flood Lab HAND models read: data/national/<dem>/hand_2000/<slug>/dem_f32.tif.

The staged dem.tif is Int16 whole metres (relief / hypsometry only). HAND in decimetres needs the unrounded heights, so
this is the same warp as national_hand.py (EPSG:3035, 30 m, bilinear, cut to the district + 3 km buffer, nodata -9999)
kept as Float32 and forced onto exactly the grid of the district's d8.tif (-te / -ts), so basin, D8 and HAND stay
pixel-aligned. The buffer ring is KEPT (the other four files are cut back to the district): a HAND window near the
district edge still has terrain to breach into.

  docker run --rm -v ./data:/data -v ./scripts:/scripts projects-dem_worker \
    python3 /scripts/national/stage_dem_f32.py --dem fab_dem [--only 5_3_2] [--force]
Then stage_terrain.py --dem <dem> (lists dem_f32.tif in the manifest as an optional file) and upload_lake.py.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

VRT = {"copernicus_dem": "/data/dem/glo30/glo30_4326_aligned.vrt", "fab_dem": "/data/dem/fabdem/fabdem_4326.vrt"}
EPSG = "EPSG:3035"
CO = ["-co", "TILED=YES", "-co", "BLOCKXSIZE=256", "-co", "BLOCKYSIZE=256", "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=3",
      "-co", "BIGTIFF=YES"]
ENCODING = {"dtype": "float32", "units": "m", "nodata": -9999, "vertical_datum": "EGM2008",
            "note": "warped DEM, bilinear, unrounded, buffered district (3 km) — Flood Lab HAND models"}


def grid_of(path: Path):
    import rasterio
    with rasterio.open(path) as r:
        return r.bounds, (r.height, r.width), r.transform


def district(a, d: dict, cuts: Path, out: Path) -> dict:
    t0 = time.time()
    d8 = out / "d8.tif"
    if not d8.exists():
        raise SystemExit(f"{d['slug']}: d8.tif missing — run national_hand.py first (the Float32 DEM must share its grid)")
    bounds, shape, _ = grid_of(d8)
    dst = out / "dem_f32.tif"
    tmp = out / "dem_f32.tmp.tif"
    subprocess.run(["gdalwarp", "-q", "-overwrite", "-t_srs", EPSG, "-te", str(bounds.left), str(bounds.bottom), str(bounds.right),
                    str(bounds.top), "-ts", str(shape[1]), str(shape[0]), "-r", "bilinear",
                    "-cutline", str(cuts / f"{d['slug']}_buf.geojson"), "-srcnodata", "-9999", "-dstnodata", "-9999",
                    "-ot", "Float32", "-multi", "-wo", "NUM_THREADS=4", "-wm", "1024", *CO, VRT[a.dem], str(tmp)], check=True)
    b2, s2, _ = grid_of(tmp)
    if s2 != shape or any(abs(x - y) > 1e-6 for x, y in zip(b2, bounds)):
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"{d['slug']}: dem_f32 grid {s2} {tuple(b2)} differs from d8 {shape} {tuple(bounds)}")
    tmp.replace(dst)
    run_path = out / "run.json"
    run = json.loads(run_path.read_text()) if run_path.exists() else {}
    run.setdefault("files", {})["dem_f32.tif"] = {k: ENCODING[k] for k in ("dtype", "units", "nodata")}
    run["dem_f32_created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_path.write_text(json.dumps(run, ensure_ascii=False, indent=1))
    return {"seconds": round(time.time() - t0, 1), "bytes": dst.stat().st_size, "shape": shape}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", choices=list(VRT), required=True)
    ap.add_argument("--threshold", type=int, default=2000)
    ap.add_argument("--root", default="/data/national")
    ap.add_argument("--only", default="", help="comma list of district slugs (e.g. 5_3_2)")
    ap.add_argument("--force", action="store_true", help="rebuild an existing dem_f32.tif")
    a = ap.parse_args()
    cuts = Path(a.root) / "_districts"
    only = {c.strip() for c in a.only.split(",") if c.strip()}
    base = Path(a.root) / a.dem / f"hand_{a.threshold}"
    for d in json.loads((cuts / "index.json").read_text()):
        if only and d["slug"] not in only:
            continue
        out = base / d["slug"]
        if (out / "dem_f32.tif").exists() and not a.force:
            print(f"skip {d['code']} (dem_f32.tif exists)", flush=True)
            continue
        print(f"{d['code']} {d['name_ua']} {d['km2']:,} km² …", flush=True)
        r = district(a, d, cuts, out)
        print(f"  dem_f32.tif {r['bytes'] / 1e6:.1f} MB, {r['shape']} in {r['seconds']:.0f} s", flush=True)


if __name__ == "__main__":
    main()
