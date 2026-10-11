"""
Regional terrain cache: DEM → hydrologically conditioned derivatives (WhiteboxTools).

One terrain per (dem_source, dem_bbox, target_epsg, pixel_size_m). Every basin click
reuses it, so only outlet snapping, watershed and per-basin statistics run per request.

Layout: /data/terrain/{key}/
    dem.tif          original elevations (EGM2008 heights for GLO-30 / FABDEM), nodata -9999
    breached.tif     breach_depressions(fill_pits=True)
    d8.tif           d8_pointer
    acc.tif          d8_flow_accumulation (cells)
    slope.tif        slope in degrees (from dem.tif)
    flowpath_len.tif max_upslope_flowpath_length (map units)
    streams_{N}.tif  extract_streams(acc >= N), 1 = stream, 0 = background
    hand_{N}.tif     elevation_above_stream for streams_{N}
    meta.json        provenance (source, bbox, crs, resolution, whitebox version)
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio

from basin_utils import download_glo30

log = logging.getLogger("terrain")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
TERRAIN_DIR = DATA_DIR / "terrain"
FABDEM_SOURCE = os.environ.get("FABDEM_SOURCE")  # GeoTIFF/VRT covering the region, EPSG:4326

DEM_INFO = {
    "copernicus_dem": {
        "label": "Copernicus GLO-30",
        "version": "Copernicus DEM GLO-30 (2019 release, AWS Open Data mirror)",
        "vertical_datum": "EGM2008",
        "attribution": "© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 "
                       "provided under COPERNICUS by the European Union and ESA",
        "licence": "Copernicus DEM licence (free for all users)",
    },
    "fab_dem": {
        "label": "FABDEM",
        "version": "FABDEM v1-2 (Hawker et al. 2022)",
        "vertical_datum": "EGM2008",
        "attribution": "FABDEM © University of Bristol",
        "licence": "CC BY-NC-SA 4.0 (non-commercial)",
    },
}

STREAM_THRESHOLDS_PRESET = (500, 1000, 2000)


def terrain_key(dem_source: str, bbox: list[float], epsg: str, res: float) -> str:
    raw = json.dumps({
        "dem_source": dem_source,
        "bbox": [round(v, 4) for v in bbox],
        "epsg": epsg.upper(),
        "res": res,
        "pipeline": "breach_fill-d8-acc-slope-v1",
    }, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _bbox_geojson(bbox: list[float], path: Path) -> Path:
    w, s, e, n = bbox
    path.write_text(json.dumps({
        "type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]},
    }))
    return path


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {proc.stderr.strip()[-800:]}")


def _wbt(wbt, name: str, ret: int) -> None:
    if ret != 0:
        raise RuntimeError(f"WhiteboxTools {name} failed (code {ret})")


@dataclass
class Terrain:
    key: str
    dir: Path
    meta: dict
    dem_source: str
    epsg: str
    res: float

    @property
    def dem(self) -> Path: return self.dir / "dem.tif"
    @property
    def breached(self) -> Path: return self.dir / "breached.tif"
    @property
    def d8(self) -> Path: return self.dir / "d8.tif"
    @property
    def acc(self) -> Path: return self.dir / "acc.tif"
    @property
    def slope(self) -> Path: return self.dir / "slope.tif"
    @property
    def flowpath_len(self) -> Path: return self.dir / "flowpath_len.tif"

    def streams(self, wbt, threshold: int) -> Path:
        """Stream raster for a flow-accumulation threshold (cells), cached."""
        p = self.dir / f"streams_{threshold}.tif"
        if not p.exists():
            log.info("terrain %s: extract_streams threshold=%d", self.key, threshold)
            _wbt(wbt, "extract_streams", wbt.extract_streams(
                str(self.acc), str(p), threshold=threshold, zero_background=True))
        return p

    def hand(self, wbt, threshold: int) -> Path:
        """HAND (elevation above nearest stream cell) for a stream threshold, cached."""
        p = self.dir / f"hand_{threshold}.tif"
        if not p.exists():
            streams = self.streams(wbt, threshold)
            log.info("terrain %s: elevation_above_stream threshold=%d", self.key, threshold)
            _wbt(wbt, "elevation_above_stream", wbt.elevation_above_stream(
                str(self.breached), str(streams), str(p)))
            with rasterio.open(p) as src:
                h = src.read(1, masked=True)
                valid = h.compressed()
                if valid.size and (valid.max() > 2000 or valid.min() < -50):
                    # window-level sanity only: large mountain windows legitimately exceed it;
                    # the binding check is per basin (hand_within_relief in the manifest)
                    log.warning(
                        f"HAND out of range ({valid.min():.0f}..{valid.max():.0f} m) — "
                        "check DEM nodata / CRS (invariant: HAND 0–50 m in the basin)")
        return p


# Local national mosaics (scripts/national): when set, DEMs are warped from these instead of
# being downloaded tile by tile. e.g. DEM_VRT_COPERNICUS_DEM=/data/dem/glo30/glo30_4326.vrt
def _local_vrt(dem_source: str) -> str | None:
    p = os.environ.get(f"DEM_VRT_{dem_source.upper()}")
    return p if p and Path(p).exists() else None


def _target_extent(bbox: list[float], epsg: str, res: float) -> list[float]:
    """Envelope of a lon/lat box in the target CRS, from a densified boundary.

    `gdalwarp -te_srs EPSG:4326` only transforms the corners; in conic/azimuthal projections
    (EPSG:3035) the box edges bow outwards, so points near the edge fell outside the DEM.
    """
    from pyproj import Transformer
    w, s, e, n = bbox
    k = 64
    lons = [w + (e - w) * i / k for i in range(k + 1)]
    lats = [s + (n - s) * i / k for i in range(k + 1)]
    ring = ([(x, s) for x in lons] + [(e, y) for y in lats] + [(x, n) for x in lons] + [(w, y) for y in lats])
    tr = Transformer.from_crs("EPSG:4326", epsg, always_xy=True)
    xs, ys = tr.transform([p[0] for p in ring], [p[1] for p in ring])
    snap = lambda v, f: f(v / res) * res
    return [snap(min(xs), math.floor), snap(min(ys), math.floor), snap(max(xs), math.ceil), snap(max(ys), math.ceil)]


def _acquire_dem(dem_source: str, bbox: list[float], epsg: str, res: float, out: Path, tmp: Path) -> None:
    bbox_gj = _bbox_geojson(bbox, tmp / "bbox.geojson")
    vrt = _local_vrt(dem_source)
    if vrt:
        # average when coarsening 30 m → ≥60 m, bilinear otherwise
        _run([
            "gdalwarp", "-overwrite", "-q", "-t_srs", epsg, "-tr", str(res), str(res),
            "-r", "average" if res >= 60 else "bilinear", "-wm", os.environ.get("GDAL_WARP_MEMORY_MB", "256"),
            "-te", *[str(v) for v in _target_extent(bbox, epsg, res)],
            "-srcnodata", "-9999", "-dstnodata", "-9999",
            "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", "-co", "BIGTIFF=IF_SAFER",
            vrt, str(out),
        ])
        return
    if dem_source == "copernicus_dem":
        download_glo30(bbox_gj, out, target_epsg=epsg, pixel_size_m=res)
        return
    if dem_source == "fab_dem":
        if not FABDEM_SOURCE or not Path(FABDEM_SOURCE).exists():
            raise RuntimeError(
                "FABDEM is not available: set FABDEM_SOURCE to a GeoTIFF/VRT covering the region "
                "(FABDEM is CC BY-NC-SA and is not auto-downloaded)")
        _run([
            "gdalwarp", "-overwrite", "-t_srs", epsg, "-tr", str(res), str(res), "-r", "bilinear",
            "-cutline", str(bbox_gj), "-crop_to_cutline", "-dstnodata", "-9999",
            "-co", "COMPRESS=DEFLATE", FABDEM_SOURCE, str(out),
        ])
        return
    raise ValueError(f"unknown dem_source {dem_source!r}")


def ensure_terrain(wbt, dem_source: str, bbox: list[float], epsg: str, res: float,
                   progress=None) -> Terrain:
    """Return the cached terrain for these parameters, building it if needed."""
    key = terrain_key(dem_source, bbox, epsg, res)
    tdir = TERRAIN_DIR / key
    tdir.mkdir(parents=True, exist_ok=True)
    meta_path = tdir / "meta.json"

    # Serialise builds of the same terrain across worker processes.
    with open(tdir / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if meta_path.exists():
                meta = json.loads(meta_path.read_text())
                if meta.get("status") == "done":
                    log.info("terrain cache hit: %s", key)
                    return Terrain(key, tdir, meta, dem_source, epsg, res)

            log.info("=== building terrain %s (%s, bbox=%s, %s @ %.0f m) ===", key, dem_source, bbox, epsg, res)
            tmp = tdir / "_tmp"
            tmp.mkdir(exist_ok=True)
            t = Terrain(key, tdir, {}, dem_source, epsg, res)

            if progress: progress(5, "downloading DEM")
            _acquire_dem(dem_source, bbox, epsg, res, t.dem, tmp)

            wbt.set_working_dir(str(tdir))
            if progress: progress(15, "breaching depressions")
            _wbt(wbt, "breach_depressions", wbt.breach_depressions(str(t.dem), str(t.breached), fill_pits=True))
            if progress: progress(25, "D8 flow direction")
            _wbt(wbt, "d8_pointer", wbt.d8_pointer(str(t.breached), str(t.d8)))
            if progress: progress(30, "D8 flow accumulation")
            _wbt(wbt, "d8_flow_accumulation", wbt.d8_flow_accumulation(str(t.breached), str(t.acc), out_type="cells"))
            if progress: progress(35, "slope")
            _wbt(wbt, "slope", wbt.slope(str(t.dem), str(t.slope), zfactor=1.0, units="degrees"))
            if progress: progress(38, "flowpath length")
            _wbt(wbt, "max_upslope_flowpath_length", wbt.max_upslope_flowpath_length(str(t.breached), str(t.flowpath_len)))
            if progress: progress(40, "streams + HAND")
            for n in STREAM_THRESHOLDS_PRESET:
                t.streams(wbt, n)
            t.hand(wbt, 2000)

            with rasterio.open(t.dem) as src:
                dem_stats = src.stats(indexes=[1], approx=False)[0]
                px = abs(src.transform.a * src.transform.e)
                meta = {
                    "status": "done",
                    "key": key,
                    "dem_source": dem_source,
                    **DEM_INFO[dem_source],
                    "bbox_wgs84": bbox,
                    "processing_crs": src.crs.to_string(),
                    "grid_resolution_m": res,
                    "pixel_area_m2": px,
                    "width": src.width, "height": src.height,
                    "elev_min_m": float(dem_stats.min), "elev_max_m": float(dem_stats.max),
                    "whitebox_version": _wbt_version(wbt),
                    "gdal_version": _gdal_version(),
                    "pipeline": ["breach_depressions(fill_pits)", "d8_pointer", "d8_flow_accumulation(cells)",
                                 "slope(degrees)", "max_upslope_flowpath_length",
                                 "extract_streams(500|1000|2000)", "elevation_above_stream"],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            meta_path.write_text(json.dumps(meta, indent=2))
            for f in tmp.iterdir():
                f.unlink()
            tmp.rmdir()
            log.info("=== terrain %s ready: %dx%d cells, elev %.0f–%.0f m ===",
                     key, meta["width"], meta["height"], meta["elev_min_m"], meta["elev_max_m"])
            t.meta = meta
            return t
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _wbt_version(wbt) -> str:
    try:
        return wbt.version().split("\n")[0].strip()
    except Exception:
        return "unknown"


def _gdal_version() -> str:
    try:
        return subprocess.run(["gdalinfo", "--version"], capture_output=True, text=True).stdout.strip()
    except Exception:
        return "unknown"
