"""
Display COGs of the national HAND for Terracotta (after national_hand.py), one DEM at a time.

  hand_ua/<dem>_cog.tif     HAND in DECIMETRES, Byte, EPSG:3857 30 m: 0–254 = 0–25.4 m, 255 = no data / higher.
                            One raster serves every water level: the frontend draws depth = level − HAND with
                            Terracotta's explicit colormap (only the listed values are drawn, the rest transparent).
  streams_ua/<dem>_cog.tif  stream cells (threshold 2000 cells), Byte 1, nodata 0, EPSG:3857 30 m.
Overviews are NEAREST (an averaged decimetre would be a wrong depth). Steps run where the tools are:
GDAL in the dem_worker image, the Byte conversion with rasterio on the host (the image's numpy cannot load gdal_array).

  python3 scripts/national/national_hand_cogs.py --dem fab_dem       # from ~/projects, host Python with rasterio
Output: data/dem_display/hand_ua/<dem>_cog.tif, data/dem_display/streams_ua/<dem>_cog.tif
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
IMAGE = "projects-dem_worker"


def gdal(*cmd: str) -> None:
    subprocess.run(["docker", "run", "--rm", "-v", f"{DATA}:/data", "--entrypoint", cmd[0], IMAGE, *cmd[1:]], check=True)


def to_decimetres(src: Path, dst: Path) -> None:
    with rasterio.open(src) as s:
        prof = s.profile | {"dtype": "uint8", "nodata": 255, "compress": "deflate", "tiled": True, "blockxsize": 512, "blockysize": 512, "BIGTIFF": "YES", "predictor": 1}
        with rasterio.open(dst, "w", **prof) as d:
            for _, w in s.block_windows(1):
                a = s.read(1, window=w, masked=True)
                v = np.where(a.mask | (a.filled(0) < 0) | (a.filled(0) > 25.44), 255, np.rint(a.filled(0) * 10)).astype("uint8")
                d.write(v, 1, window=w)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", required=True)
    ap.add_argument("--threshold", type=int, default=2000)
    a = ap.parse_args()
    base = f"/data/national/{a.dem}/hand_{a.threshold}"
    tmp = DATA / "dem_display" / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    for kind in ("hand_ua", "streams_ua"):
        (DATA / "dem_display" / kind).mkdir(parents=True, exist_ok=True)
    # 1) mosaics of the districts (they do not overlap), reprojected to Web Mercator
    gdal("sh", "-c", f"gdalbuildvrt -q -overwrite /data/dem_display/_tmp/{a.dem}_hand.vrt {base}/*/hand.tif && "
                     f"gdalbuildvrt -q -overwrite /data/dem_display/_tmp/{a.dem}_streams.vrt {base}/*/streams.tif")
    co = ["-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", "-co", "BIGTIFF=YES", "-multi", "-wo", "NUM_THREADS=8", "-wm", "4096"]
    gdal("gdalwarp", "-q", "-overwrite", "-t_srs", "EPSG:3857", "-tr", "30", "30", "-r", "near", "-srcnodata", "-9999",
         "-dstnodata", "-9999", "-ot", "Float32", *co, f"/data/dem_display/_tmp/{a.dem}_hand.vrt", f"/data/dem_display/_tmp/{a.dem}_hand_3857.tif")
    gdal("gdalwarp", "-q", "-overwrite", "-t_srs", "EPSG:3857", "-tr", "30", "30", "-r", "max", "-srcnodata", "0",
         "-dstnodata", "0", "-ot", "Byte", *co, f"/data/dem_display/_tmp/{a.dem}_streams.vrt", f"/data/dem_display/_tmp/{a.dem}_streams_3857.tif")
    # 2) HAND → decimetres (host rasterio)
    to_decimetres(tmp / f"{a.dem}_hand_3857.tif", tmp / f"{a.dem}_hand_dm.tif")
    # 3) COGs with NEAREST overviews
    cog = ["-of", "COG", "-co", "COMPRESS=DEFLATE", "-co", "BLOCKSIZE=512", "-co", "OVERVIEW_RESAMPLING=NEAREST", "-co", "BIGTIFF=YES", "-co", "NUM_THREADS=8"]
    gdal("gdal_translate", "-q", *cog, f"/data/dem_display/_tmp/{a.dem}_hand_dm.tif", f"/data/dem_display/hand_ua/{a.dem}_cog.tif")
    gdal("gdal_translate", "-q", *cog, f"/data/dem_display/_tmp/{a.dem}_streams_3857.tif", f"/data/dem_display/streams_ua/{a.dem}_cog.tif")
    for f in tmp.glob(f"{a.dem}_*"):
        f.unlink()
    for kind in ("hand_ua", "streams_ua"):
        p = DATA / "dem_display" / kind / f"{a.dem}_cog.tif"
        print(kind, p, f"{p.stat().st_size / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
