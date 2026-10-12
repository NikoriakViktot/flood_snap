"""
Slope (degrees) over the WHOLE DEM mosaic (21–41° E, 44–57° N: Ukraine and the transboundary headwaters in the
neighbouring countries), not cut to the country or to the river-basin districts. EPSG:3035, 30 m, in a grid of
chunks with an overlap so chunk edges carry no artefacts; stored as Byte = round(slope° × 2) (0.5° steps, 0..180,
nodata 255), which is enough for FFPI classes and display.

  docker run --rm -v ./data:/data -v ./scripts:/scripts projects-dem_worker \\
    python3 /scripts/national/national_slope.py --dem fab_dem [--chunks 5]
Outputs: data/national/<dem>/slope_full/<row>_<col>.tif + index.json (resumable per chunk)
Then:    python3 scripts/national/national_slope_cogs.py --dem fab_dem   (host; mosaic → EPSG:3857 COG slope_ua/)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

VRT = {"copernicus_dem": "/data/dem/glo30/glo30_4326_aligned.vrt", "fab_dem": "/data/dem/fabdem/fabdem_4326.vrt"}
EPSG, RES = "EPSG:3035", 30.0
CO = ["-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", "-co", "BIGTIFF=YES"]
OVERLAP = 8          # cells of overlap on every side of a chunk (slope needs 1; keep a margin for -tap rounding)


def sh(*args: str) -> None:
    subprocess.run(list(args), check=True)


def full_extent_3035(vrt: str) -> tuple[float, float, float, float]:
    """Bounding box of the 4326 mosaic in EPSG:3035, snapped outwards to the 30 m grid."""
    from osgeo import gdal, osr
    ds = gdal.Open(vrt)
    gt = ds.GetGeoTransform()
    w, h = ds.RasterXSize, ds.RasterYSize
    x0, y1, x1, y0 = gt[0], gt[3], gt[0] + gt[1] * w, gt[3] + gt[5] * h
    src = osr.SpatialReference(); src.ImportFromEPSG(4326); src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference(); dst.ImportFromEPSG(3035); dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src, dst)
    pts = [tr.TransformPoint(x, y) for x in np.linspace(x0, x1, 41) for y in np.linspace(y0, y1, 41)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    snap = lambda v, up: (np.ceil(v / RES) if up else np.floor(v / RES)) * RES
    return snap(min(xs), False), snap(min(ys), False), snap(max(xs), True), snap(max(ys), True)


def main():
    import rasterio
    from rasterio.windows import Window

    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", choices=list(VRT), default="fab_dem")
    ap.add_argument("--chunks", type=int, default=5, help="chunks per axis")
    ap.add_argument("--root", default="/data/national")
    a = ap.parse_args()
    out = Path(a.root) / a.dem / "slope_full"
    work = out / "_work"
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(exist_ok=True)
    x0, y0, x1, y1 = full_extent_3035(VRT[a.dem])
    nx, ny = int((x1 - x0) / RES), int((y1 - y0) / RES)
    cw, ch = -(-nx // a.chunks), -(-ny // a.chunks)
    index = {"dem": a.dem, "crs": EPSG, "res_m": RES, "encoding": "byte_half_degrees", "nodata": 255,
             "extent_3035": [x0, y0, x1, y1], "chunks": []}
    print(f"{a.dem}: {nx}×{ny} cells, {a.chunks}×{a.chunks} chunks of {cw}×{ch}", flush=True)
    for r in range(a.chunks):
        for c in range(a.chunks):
            name = f"{r}_{c}"
            dst = out / f"{name}.tif"
            cx0, cy1 = x0 + c * cw * RES, y1 - r * ch * RES
            cx1, cy0 = min(cx0 + cw * RES, x1), max(cy1 - ch * RES, y0)
            index["chunks"].append({"name": name, "bbox_3035": [cx0, cy0, cx1, cy1]})
            if dst.exists():
                continue
            t0 = time.time()
            te = [str(cx0 - OVERLAP * RES), str(cy0 - OVERLAP * RES), str(cx1 + OVERLAP * RES), str(cy1 + OVERLAP * RES)]
            dem, slope = work / f"{name}_dem.tif", work / f"{name}_slope.tif"
            sh("gdalwarp", "-q", "-overwrite", "-t_srs", EPSG, "-tr", str(RES), str(RES), "-te", *te, "-r", "bilinear",
               "-srcnodata", "-9999", "-dstnodata", "-9999", "-ot", "Float32", "-multi", "-wo", "NUM_THREADS=8", "-wm", "2048",
               *CO, VRT[a.dem], str(dem))
            import whitebox
            w = whitebox.WhiteboxTools()
            w.set_verbose_mode(False)
            if w.slope(str(dem), str(slope), units="degrees") != 0:
                raise RuntimeError(f"{name}: WhiteboxTools slope failed")
            with rasterio.open(slope) as s:
                win = Window(OVERLAP, OVERLAP, s.width - 2 * OVERLAP, s.height - 2 * OVERLAP)
                arr = s.read(1, window=win, masked=True)
                prof = dict(driver="GTiff", width=int(win.width), height=int(win.height), count=1, dtype="uint8", crs=s.crs,
                            transform=s.window_transform(win), nodata=255, compress="deflate", tiled=True, blockxsize=512,
                            blockysize=512, BIGTIFF="YES")
            data = np.where(np.ma.getmaskarray(arr), 255, np.clip(np.rint(arr.filled(0) * 2), 0, 180)).astype("uint8")
            tmp = out / f"{name}.tmp.tif"
            with rasterio.open(tmp, "w", **prof) as d:
                d.write(data, 1)
            tmp.replace(dst)
            for f in (dem, slope):
                f.unlink(missing_ok=True)
            valid = int((data != 255).sum())
            print(f"  {name} done in {time.time() - t0:.0f} s, valid {valid / 1e6:.0f}M px", flush=True)
    index["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (out / "index.json").write_text(json.dumps(index, indent=1))
    work.rmdir()


if __name__ == "__main__":
    main()
