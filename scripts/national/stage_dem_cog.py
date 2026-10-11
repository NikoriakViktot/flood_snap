"""
Stage national DEM tiles as COGs in the S3 data-lake layout (local dir, then upload_lake.py).

  <staging>/hydro/lake/dem/source=copernicus_glo30/release=aws-open-data/crs=EPSG4326/
      tiles/Copernicus_DSM_COG_10_N47_00_E024_00_DEM.tif   original COGs, bit-exact (hard links)
      mosaic.vrt          VRT over tiles/ with relative paths (works locally and via /vsis3/)
      tile_index.geojson  one polygon per tile with sha256 and size
      manifest.json       source, licence, datum, coverage, counts, checksums
  <staging>/hydro/lake/dem/source=fabdem/release=v1-2/crs=EPSG4326/
      tiles/N47E024_FABDEM_V1-2.tif   re-encoded COG (DEFLATE+PREDICTOR=3, 512 blocks, overviews,
                                      nodata -9999); FABDEM ships without overviews

Run inside the dem_worker image (GDAL):
  docker run --rm -v ./data:/data -v ./scripts:/scripts projects-dem_worker \
    python3 /scripts/national/stage_dem_cog.py --workers 8
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

SOURCES = {
    "copernicus_glo30": {
        "src": "/data/dem/glo30/tiles", "pattern": r"Copernicus_DSM_COG_10_([NS])(\d\d)_00_([EW])(\d{3})_00_DEM\.tif",
        "prefix": "hydro/lake/dem/source=copernicus_glo30/release=aws-open-data/crs=EPSG4326",
        "reencode": False,
        "meta": {
            "product": "Copernicus DEM GLO-30 (DSM)", "origin": "s3://copernicus-dem-30m (AWS Open Data)",
            "licence": "Copernicus DEM licence — free for all users",
            "attribution": "© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under "
                           "COPERNICUS by the European Union and ESA; all rights reserved",
            "vertical_datum": "EGM2008", "crs": "EPSG:4326", "resolution": "1 arcsec (lon 1.5\" above 50°N)",
        },
    },
    "fabdem": {
        "src": "/data/dem/fabdem/tiles", "pattern": r"([NS])(\d\d)([EW])(\d{3})_FABDEM_V1-2\.tif",
        "prefix": "hydro/lake/dem/source=fabdem/release=v1-2/crs=EPSG4326",
        "reencode": True,
        "meta": {
            "product": "FABDEM v1-2 (Forest And Buildings removed Copernicus DEM)",
            "origin": "University of Bristol data.bris.ac.uk (via the fabdem package)",
            "licence": "CC BY-NC-SA 4.0 — non-commercial use only",
            "attribution": "Hawker et al. (2022) FABDEM, University of Bristol",
            "vertical_datum": "EGM2008", "crs": "EPSG:4326", "resolution": "1 arcsec",
        },
    },
}


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tile_bounds(m) -> list[float]:
    ns, lat, ew, lon = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
    lat = lat if ns == "N" else -lat
    lon = lon if ew == "E" else -lon
    return [lon, lat, lon + 1, lat + 1]


def stage_tile(src: Path, dst: Path, reencode: bool) -> None:
    if dst.exists():
        return
    if not reencode:
        try:
            os.link(src, dst)          # same filesystem: no copy, bit-exact
        except OSError:
            subprocess.run(["cp", "--reflink=auto", str(src), str(dst)], check=True)
        return
    tmp = dst.with_suffix(".part.tif")
    subprocess.run([
        "gdal_translate", "-q", "-of", "COG", "-a_nodata", "-9999",
        "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=3", "-co", "BLOCKSIZE=512",
        "-co", "OVERVIEWS=IGNORE_EXISTING", "-co", "RESAMPLING=AVERAGE", "-co", "NUM_THREADS=2",
        str(src), str(tmp)], check=True)
    tmp.replace(dst)


def stage(name: str, cfg: dict, staging: Path, workers: int) -> dict:
    out = staging / cfg["prefix"]
    tiles_dir = out / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    rx = re.compile(cfg["pattern"])
    srcs = sorted(p for p in Path(cfg["src"]).glob("*.tif") if rx.fullmatch(p.name))
    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(lambda p: stage_tile(p, tiles_dir / p.name, cfg["reencode"]), srcs))

    feats, entries = [], []
    for p in sorted(tiles_dir.glob("*.tif")):
        m = rx.fullmatch(p.name)
        if not m:
            continue
        w, s, e, n = tile_bounds(m)
        rec = {"tile": p.name, "bytes": p.stat().st_size, "sha256": sha256(p), "bbox": [w, s, e, n]}
        entries.append(rec)
        feats.append({"type": "Feature", "properties": {k: rec[k] for k in ("tile", "bytes", "sha256")},
                      "geometry": {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}})
    (out / "tile_index.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))

    # relative paths in the VRT so the same file works from the bucket (/vsis3/<bucket>/<prefix>/mosaic.vrt)
    lst = out / ".vrt_inputs.txt"
    lst.write_text("\n".join(f"tiles/{e['tile']}" for e in entries) + "\n")
    subprocess.run(["gdalbuildvrt", "-q", "-overwrite", "-resolution", "highest", "-vrtnodata", "-9999",
                    "-input_file_list", lst.name, "mosaic.vrt"], check=True, cwd=out)
    lst.unlink()
    vrt = (out / "mosaic.vrt").read_text().replace(f"{out}/", "")
    vrt = vrt.replace('relativeToVRT="0"', 'relativeToVRT="1"')
    (out / "mosaic.vrt").write_text(vrt)

    lons = [e["bbox"][0] for e in entries] + [e["bbox"][2] for e in entries]
    lats = [e["bbox"][1] for e in entries] + [e["bbox"][3] for e in entries]
    manifest = {
        "schema": "dem.tiles.v1", "source": name, **cfg["meta"],
        "format": "COG (Cloud Optimized GeoTIFF), Float32, nodata -9999" if cfg["reencode"]
                  else "COG as distributed (Float32, DEFLATE, overviews)",
        "reencoded": cfg["reencode"],
        "coverage_note": "Ukraine 21–41°E 44–53°N + upper Dnipro/Desna/Pripyat basin 23–37°E 53–56.5°N; "
                         "Danube basin excluded; sea tiles do not exist",
        "bbox": [min(lons), min(lats), max(lons), max(lats)],
        "tiles": len(entries), "bytes": sum(e["bytes"] for e in entries),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": {"mosaic": "mosaic.vrt", "index": "tile_index.geojson", "tiles_dir": "tiles/"},
        "checksums": {f"tiles/{e['tile']}": e["sha256"] for e in entries},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return {"source": name, "tiles": len(entries), "GiB": round(manifest["bytes"] / 2**30, 2), "dir": str(out)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--staging", default="/data/s3_staging")
    ap.add_argument("--only", choices=list(SOURCES), default=None)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    for name, cfg in SOURCES.items():
        if a.only and name != a.only:
            continue
        print(json.dumps(stage(name, cfg, Path(a.staging), a.workers)), flush=True)


if __name__ == "__main__":
    main()
