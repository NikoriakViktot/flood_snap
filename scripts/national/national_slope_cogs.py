"""
Display COG of the slope (national_slope.py chunks: Byte, half degrees, nodata 255; the whole DEM mosaic, not cut to
Ukraine) for Terracotta: slope_ua/<dem>_cog.tif, EPSG:3857 30 m, NEAREST overviews. The frontend colours it with an
explicit colormap (value / 2 = degrees).

  python3 scripts/national/national_slope_cogs.py --dem fab_dem       # from ~/projects (GDAL in the worker image)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", default="fab_dem")
    a = ap.parse_args()
    (DATA / "dem_display" / "slope_ua").mkdir(parents=True, exist_ok=True)
    (DATA / "dem_display" / "_tmp").mkdir(parents=True, exist_ok=True)
    gdal("sh", "-c", f"gdalbuildvrt -q -overwrite /data/dem_display/_tmp/{a.dem}_slope.vrt /data/national/{a.dem}/slope_full/*.tif")
    co = ["-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", "-co", "BIGTIFF=YES", "-multi", "-wo", "NUM_THREADS=8", "-wm", "4096"]
    gdal("gdalwarp", "-q", "-overwrite", "-t_srs", "EPSG:3857", "-tr", "30", "30", "-r", "near", "-srcnodata", "255", "-dstnodata", "255",
         "-ot", "Byte", *co, f"/data/dem_display/_tmp/{a.dem}_slope.vrt", f"/data/dem_display/_tmp/{a.dem}_slope_3857.tif")
    gdal("gdal_translate", "-q", "-of", "COG", "-co", "COMPRESS=DEFLATE", "-co", "BLOCKSIZE=512", "-co", "OVERVIEW_RESAMPLING=NEAREST",
         "-co", "BIGTIFF=YES", "-co", "NUM_THREADS=8", f"/data/dem_display/_tmp/{a.dem}_slope_3857.tif", f"/data/dem_display/slope_ua/{a.dem}_cog.tif")
    for f in (DATA / "dem_display" / "_tmp").glob(f"{a.dem}_slope*"):
        f.unlink()
    out = DATA / "dem_display" / "slope_ua" / f"{a.dem}_cog.tif"
    print(out, f"{out.stat().st_size / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
