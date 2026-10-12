"""
FFPI variants from the factor rasters of national_ffpi.py, without recomputing slope, land cover or soils: the index
is a weighted mean of the factors, so a new weighting only needs S, L, H, V of each district.

  ffpi_<tag>/<slug>/ffpi.tif   FFPI × 10, Byte, nodata 0 (same grid and valid area as the v1 raster)
  ffpi_<tag>/<slug>/run.json   weights, histogram

The v1 rasters (ffpi/<slug>/) stay as they are, for comparison. A weight 0 drops the factor (v2 drops V, which
duplicated WorldCover: L and V were both derived from it).

  docker run --rm -v ./data:/data -v ./scripts:/scripts projects-dem_worker \
    python3 /scripts/national/ffpi_recombine.py --tag v2 --weights 2,1,1,0
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


def main():
    import rasterio
    from rasterio.windows import Window

    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", default="fab_dem")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--weights", required=True, help="s,l,h,v e.g. 2,1,1,0")
    ap.add_argument("--root", default="/data/national")
    a = ap.parse_args()
    w = np.asarray([float(x) for x in a.weights.split(",")], dtype=np.float32)
    assert w.size == 4 and w.sum() > 0
    src_base = Path(a.root) / a.dem / "ffpi"
    out_base = Path(a.root) / a.dem / f"ffpi_{a.tag}"
    for d in sorted(p for p in src_base.iterdir() if p.is_dir() and (p / "ffpi.tif").exists()):
        out = out_base / d.name
        if (out / "ffpi.tif").exists():
            continue
        out.mkdir(parents=True, exist_ok=True)
        hist = np.zeros(101, np.int64)
        srcs = [rasterio.open(d / f"{n}.tif") for n in ("s", "l", "h", "v")]
        prof = dict(srcs[0].profile, dtype="uint8", nodata=0, compress="deflate", tiled=True, blockxsize=512, blockysize=512, BIGTIFF="YES")
        tmp = out / "ffpi.tmp.tif"
        with rasterio.open(tmp, "w", **prof) as dst:
            H, W = srcs[0].height, srcs[0].width
            for row in range(0, H, 1024):
                win = Window(0, row, W, min(1024, H - row))
                f = [s.read(1, window=win).astype(np.float32) for s in srcs]
                valid = f[0] > 0
                v = np.rint((w[0] * f[0] + w[1] * f[1] + w[2] * f[2] + w[3] * f[3]) / w.sum() * 10)
                arr = np.where(valid, np.clip(v, 1, 100), 0).astype(np.uint8)
                dst.write(arr, 1, window=win)
                hist += np.bincount(arr.ravel(), minlength=101)[:101]
        for s in srcs:
            s.close()
        tmp.replace(out / "ffpi.tif")
        (out / "run.json").write_text(json.dumps({"district": d.name, "tag": a.tag, "weights": dict(zip("slhv", w.tolist())),
                                                  "histogram_x10": hist.tolist(), "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}))
        print(d.name, "mean", round(float((np.arange(101)[1:] * hist[1:]).sum() / max(hist[1:].sum(), 1)) / 10, 3), flush=True)


if __name__ == "__main__":
    main()
