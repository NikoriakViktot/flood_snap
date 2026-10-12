"""
Display COGs of the national FFPI for Terracotta (after national_ffpi.py), one DEM at a time.

  ffpi_ua/<dem>_cog.tif        FFPI × 10, Byte 10..100, nodata 0, EPSG:3857 30 m (the frontend divides by 10 and
                               draws the five classes with an explicit colormap).
  ffpi_{s,l,h,v}_ua/<dem>_cog.tif   the factors 1..10 (--factors).
Overviews are NEAREST (an averaged class boundary would be a wrong class). GDAL runs in the dem_worker image.

  python3 scripts/national/national_ffpi_cogs.py --dem fab_dem [--factors]      # from ~/projects
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
IMAGE = "projects-dem_worker"


def gdal(*cmd: str) -> None:
    subprocess.run(["docker", "run", "--rm", "-v", f"{DATA}:/data", "--entrypoint", cmd[0], IMAGE, *cmd[1:]], check=True)


def build(dem: str, name: str, category: str, tag: str = "") -> Path:
    tmp = DATA / "dem_display" / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    (DATA / "dem_display" / category).mkdir(parents=True, exist_ok=True)
    base = f"/data/national/{dem}/ffpi_{tag}" if tag and name == "ffpi" else f"/data/national/{dem}/ffpi"
    gdal("sh", "-c", f"gdalbuildvrt -q -overwrite /data/dem_display/_tmp/{dem}_{name}.vrt {base}/[!_]*/{name}.tif")
    co = ["-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", "-co", "BIGTIFF=YES", "-multi", "-wo", "NUM_THREADS=8", "-wm", "4096"]
    gdal("gdalwarp", "-q", "-overwrite", "-t_srs", "EPSG:3857", "-tr", "30", "30", "-r", "near", "-srcnodata", "0",
         "-dstnodata", "0", "-ot", "Byte", *co, f"/data/dem_display/_tmp/{dem}_{name}.vrt", f"/data/dem_display/_tmp/{dem}_{name}_3857.tif")
    cog = ["-of", "COG", "-co", "COMPRESS=DEFLATE", "-co", "BLOCKSIZE=512", "-co", "OVERVIEW_RESAMPLING=NEAREST", "-co", "BIGTIFF=YES", "-co", "NUM_THREADS=8"]
    gdal("gdal_translate", "-q", *cog, f"/data/dem_display/_tmp/{dem}_{name}_3857.tif", f"/data/dem_display/{category}/{dem}_cog.tif")
    for f in tmp.glob(f"{dem}_{name}*"):
        f.unlink()
    out = DATA / "dem_display" / category / f"{dem}_cog.tif"
    print(category, out, f"{out.stat().st_size / 1e6:.0f} MB")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", default="fab_dem")
    ap.add_argument("--factors", action="store_true", help="also the s, l, h, v factor rasters")
    ap.add_argument("--tag", default="", help="variant of ffpi_recombine.py for the index raster (e.g. v2)")
    a = ap.parse_args()
    build(a.dem, "ffpi", "ffpi_ua", a.tag)
    if a.factors:
        for n in ("s", "l", "h", "v"):
            build(a.dem, n, f"ffpi_{n}_ua")


if __name__ == "__main__":
    main()
