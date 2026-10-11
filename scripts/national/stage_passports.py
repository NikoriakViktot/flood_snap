"""
Stage per-gauge basin passports in the S3 lake layout (the manifest_key in the GeoParquet points here):

  data/national/<dem>/gauges/post_id_<id>/…  →  <staging>/hydro/lake/basin_delineation/v1/gauges/
                                                 dem_source=<dem>/post_id=<id>/…

Locally the directories use "_" (WhiteboxTools splits CLI args on "="), in the lake the usual
hive "key=value". Files are hard-linked when possible (no copy). Run in the dem_worker image so the
container-owned outputs can be linked:
  docker run --rm -v ./data:/data -v ./scripts:/scripts projects-dem_worker \
    python3 /scripts/national/stage_passports.py --dem copernicus_dem
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

LAKE = "hydro/lake/basin_delineation/v1/gauges"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", default="copernicus_dem")
    ap.add_argument("--root", default="/data/national")
    ap.add_argument("--staging", default="/data/s3_staging")
    ap.add_argument("--owner", default="1000:1000", help="chown staged files (host user)")
    a = ap.parse_args()

    src_root = Path(a.root) / a.dem / "gauges"
    dst_root = Path(a.staging) / LAKE / f"dem_source={a.dem}"
    uid, gid = (int(x) for x in a.owner.split(":"))
    n_dirs = n_files = n_bytes = 0
    for d in sorted(src_root.glob("post_id_*")):
        if not (d / "manifest.json").exists():
            continue                        # unfinished or rejected gauge
        pid = d.name.split("_")[-1]
        out = dst_root / f"post_id={pid}"
        if out.exists():
            shutil.rmtree(out)
        for f in d.rglob("*"):
            if not f.is_file() or "_tmp" in f.parts:
                continue
            t = out / f.relative_to(d)
            t.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(f, t)
            except OSError:
                shutil.copy2(f, t)
            n_files += 1
            n_bytes += f.stat().st_size
        n_dirs += 1
    for p in [dst_root, *dst_root.rglob("*")]:
        os.chown(p, uid, gid)
    print(f"staged {n_dirs} passports, {n_files} files, {n_bytes / 2**30:.2f} GiB → {dst_root}", flush=True)


if __name__ == "__main__":
    main()
