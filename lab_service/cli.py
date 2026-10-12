"""Run the Lab pipeline locally, without Redis or Django:
  python3 -m lab_service.cli --region 5.3.2 --lon 24.47 --lat 48.30 [--dems fab_dem]
  python3 -m lab_service.cli --district-dir /data/national/fab_dem/hand_2000/5_1_1 --lon 30.5 --lat 52.1   (raw folder)
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from lab_service import settings
from lab_service import pipeline as P


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--region")
    ap.add_argument("--district-dir", help="folder with d8/streams/hand/dem.tif (one DEM, no comparison)")
    ap.add_argument("--dems", default=",".join(settings.DEMS))
    a = ap.parse_args()
    import whitebox
    wbt = whitebox.WhiteboxTools()
    wbt.set_verbose_mode(False)
    tmp = Path(tempfile.mkdtemp(prefix="lab_"))
    runs = {}
    if a.district_dir:
        d = Path(a.district_dir)
        paths = {n: d / f"{n}.tif" for n in ("d8", "streams", "hand", "dem")}
        runs["single"] = P.run_one(wbt, "single", paths, a.lon, a.lat, tmp / "single", progress=lambda s: print("..", s))
    else:
        from lab_service.terrain import TerrainStore
        t = TerrainStore()
        district = t.district_by_code(a.region) if a.region else t.find_district(a.lon, a.lat)
        if district is None:
            raise SystemExit("no district")
        for dem in a.dems.split(","):
            runs[dem] = P.run_one(wbt, dem, t.paths(dem, district.slug), a.lon, a.lat, tmp / dem, progress=lambda s, dem=dem: print("..", dem, s))
    for dem, r in runs.items():
        s = {k: v for k, v in r["stats"].items() if k not in ("hypsometry", "hand_hist")}
        print(dem, json.dumps(s, ensure_ascii=False))
    if len(runs) == 2:
        a_, b_ = runs.values()
        c = P.compare(a_, b_)
        print("comparison", json.dumps({k: v for k, v in c.items() if k != "secondary_basin"}, ensure_ascii=False))
    P.cleanup(tmp)


if __name__ == "__main__":
    main()
