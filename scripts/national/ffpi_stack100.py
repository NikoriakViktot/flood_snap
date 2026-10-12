"""
National 100 m statistics stack of the FFPI (EPSG:3035), the input of ffpi_zonal.py. Built from the 30 m district
rasters of national_ffpi.py, so every zonal statistic is a weighted mean of exact 30 m fractions and no polygon
(hromada, gauge basin) is cut by a district edge. Runs in the dem_worker image (GDAL + rasterio).

  _stack100/ffpi_mean.tif   mean FFPI × 10 of the valid 30 m cells (Float32, 0 = no valid cell)
  _stack100/valid.tif       fraction of the 100 m cell that is valid FFPI (land inside the computed area)
  _stack100/high7.tif       fraction with FFPI >= 7            high85.tif  fraction with FFPI >= 8.5
  _stack100/builtup.tif     fraction built-up (factor L = 10 <=> WorldCover 50)
  _stack100/{s,l,h,v}_mean.tif   mean factor (Float32, 0 = none)
  _stack100/pop.tif         WorldPop 2020 constrained people per 100 m cell (sum-reprojected)
  _stack100/national_hist.json   histogram of FFPI × 10 over every valid 30 m cell

  docker run --rm -v ./data:/data -v ./scripts:/scripts projects-dem_worker python3 /scripts/national/ffpi_stack100.py --dem fab_dem
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np

CO = ["-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", "-co", "BIGTIFF=YES"]
WARP = ["-multi", "-wo", "NUM_THREADS=8", "-wm", "2048"]
POP = "/data/national/_sources/ukr_population_2020_100m_constrained_v2/ukr_pop_2020_100m_constrained_v2.tif"


def sh(*args: str) -> None:
    subprocess.run(list(args), check=True)


def calc(op: str, value: int, src: str, dst: str) -> None:
    """Byte mask (1/0) of src `op` value, block by block with rasterio (gdal_calc cannot load gdal_array in the
    worker image: its numpy is 2.x, GDAL's bindings were built for 1.x)."""
    import operator
    import rasterio
    f = {"gt": operator.gt, "ge": operator.ge, "eq": operator.eq}[op]
    with rasterio.open(src) as s:
        prof = dict(driver="GTiff", width=s.width, height=s.height, count=1, dtype="uint8", crs=s.crs, transform=s.transform,
                    nodata=None, compress="deflate", tiled=True, blockxsize=512, blockysize=512, BIGTIFF="YES")
        with rasterio.open(dst, "w", **prof) as d:
            for row in range(0, s.height, 1024):
                win = rasterio.windows.Window(0, row, s.width, min(1024, s.height - row))
                d.write(f(s.read(1, window=win), value).astype("uint8"), 1, window=win)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", default="fab_dem")
    ap.add_argument("--root", default="/data/national")
    ap.add_argument("--force", action="store_true", help="rebuild layers that already exist")
    ap.add_argument("--tag", default="", help="a variant of ffpi_recombine.py: only the FFPI layers are rebuilt from "
                                              "ffpi_<tag>/, the factor, built-up and population layers are linked from the v1 stack")
    a = ap.parse_args()
    base = Path(a.root) / a.dem / "ffpi"
    stack = base / "_stack100"
    if a.tag:
        import os
        v1 = stack
        base_ffpi = Path(a.root) / a.dem / f"ffpi_{a.tag}"
        stack = base_ffpi / "_stack100"
        stack.mkdir(parents=True, exist_ok=True)
        for n in ("s_mean", "l_mean", "h_mean", "v_mean", "builtup", "pop"):
            if not (stack / f"{n}.tif").exists():
                os.link(v1 / f"{n}.tif", stack / f"{n}.tif")
    tmp = stack / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    vrt = {}
    for n in (("ffpi",) if a.tag else ("ffpi", "s", "l", "h", "v")):
        vrt[n] = tmp / f"{n}.vrt"
        src_dir = (Path(a.root) / a.dem / f"ffpi_{a.tag}") if (a.tag and n == "ffpi") else base
        sh("sh", "-c", f"gdalbuildvrt -q -overwrite {vrt[n]} {src_dir}/[!_]*/{n}.tif")
    # means of the valid 30 m cells (nodata 0 is ignored by -r average)
    done = lambda n: (stack / f"{n}.tif").exists() and not a.force
    if not done("ffpi_mean"):
        sh("gdalwarp", "-q", "-overwrite", "-tr", "100", "100", "-tap", "-r", "average", "-srcnodata", "0", "-dstnodata", "0",
           "-ot", "Float32", *WARP, *CO, str(vrt["ffpi"]), str(stack / "ffpi_mean.tif"))
    import rasterio
    with rasterio.open(stack / "ffpi_mean.tif") as r:
        b, w, h = r.bounds, r.width, r.height
    te = ["-te", str(b.left), str(b.bottom), str(b.right), str(b.top), "-ts", str(w), str(h)]
    for n in ("s", "l", "h", "v"):
        if a.tag or done(f"{n}_mean"):
            continue
        sh("gdalwarp", "-q", "-overwrite", *te, "-r", "average", "-srcnodata", "0", "-dstnodata", "0", "-ot", "Float32",
           *WARP, *CO, str(vrt[n]), str(stack / f"{n}_mean.tif"))
    # fractions of the whole 100 m cell (nodata counted as 0, so a cell outside the land shows the gap)
    masks = {"valid": ("gt", 0, "ffpi"), "high7": ("ge", 70, "ffpi"), "high85": ("ge", 85, "ffpi")}
    if not a.tag:
        masks["builtup"] = ("eq", 10, "l")
    for n, (op, value, src) in masks.items():
        if done(n):
            continue
        m30 = tmp / f"{n}_30.tif"
        calc(op, value, str(vrt[src]), str(m30))
        sh("gdalwarp", "-q", "-overwrite", *te, "-r", "average", "-ot", "Float32", *WARP, *CO, str(m30), str(stack / f"{n}.tif"))
        m30.unlink()
    if a.tag:
        pass
    elif not done("pop"):
        # population: sum-reprojection of the 100 m WorldPop grid (nodata -99999 ignored)
        sh("gdalwarp", "-q", "-overwrite", "-t_srs", "EPSG:3035", *te, "-r", "sum", "-srcnodata", "-99999", "-dstnodata", "0",
           "-ot", "Float32", *WARP, *CO, POP, str(stack / "pop.tif"))
    # national histogram over the 30 m mosaic
    hist = np.zeros(101, np.int64)
    with rasterio.open(vrt["ffpi"]) as r:
        for _, win in r.block_windows(1):
            hist += np.bincount(r.read(1, window=win).ravel(), minlength=101)[:101]
    (stack / "national_hist.json").write_text(json.dumps({"dem": a.dem, "res_m": 30, "histogram_x10": hist.tolist(),
                                                           "valid_px": int(hist[1:].sum())}))
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    print("stack100:", sorted(p.name for p in stack.iterdir()), "valid px", int(hist[1:].sum()))


if __name__ == "__main__":
    main()
