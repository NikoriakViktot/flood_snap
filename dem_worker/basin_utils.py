"""
Shared utilities for DEM/SAR workers.

- basin_hash: deterministic cache key from basin GeoJSON geometry
- basin_bbox: extract bounding box from GeoJSON
- download_glo30: download + mosaic Copernicus GLO-30 DEM tiles
- extract_dem_from_snap_tc: extract elevation band from SNAP TC output (fallback)
- warp_to_match: gdalwarp src to match ref grid exactly
"""

import hashlib
import json
import logging
import math
import os
import subprocess
import urllib.request
from pathlib import Path

import shutil

import numpy as np
import rasterio

log = logging.getLogger("basin_utils")

# Copernicus GLO-30 DEM: public S3 bucket (no auth needed)
GLO30_BASE_URL = "https://copernicus-dem-30m.s3.amazonaws.com"


# Canonical implementations live in shared.basin.
# Re-exported here for backward compatibility.
try:
    from shared.basin import basin_hash, basin_bbox, _extract_geometry, _flatten_coords
except ImportError:
    # Fallback: inline implementations when shared is not on sys.path
    def _extract_geometry(data: dict) -> dict:
        if data.get("type") == "FeatureCollection":
            return data["features"][0]["geometry"]
        elif data.get("type") == "Feature":
            return data["geometry"]
        return data

    def _flatten_coords(geom: dict) -> list[list[float]]:
        coords = geom.get("coordinates", [])
        result = []
        def _recurse(item):
            if isinstance(item, (list, tuple)):
                if len(item) >= 2 and isinstance(item[0], (int, float)):
                    result.append(item)
                else:
                    for sub in item:
                        _recurse(sub)
        _recurse(coords)
        return result

    def basin_hash(geojson_path):
        with open(geojson_path) as f:
            data = json.load(f)
        geom = _extract_geometry(data)
        raw = json.dumps(geom, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def basin_bbox(geojson_path):
        with open(geojson_path) as f:
            data = json.load(f)
        geom = _extract_geometry(data)
        coords = _flatten_coords(geom)
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        return (min(lons), min(lats), max(lons), max(lats))


# ---------------------------------------------------------------------------
# Copernicus GLO-30 DEM
# ---------------------------------------------------------------------------

def glo30_tile_names(west: float, south: float, east: float, north: float) -> list[str]:
    """Compute GLO-30 tile names covering a bounding box.

    GLO-30 tiles are 1°×1°. Tile name encodes the SW corner:
      Copernicus_DSM_COG_10_N47_00_E024_00_DEM

    The latitude in the name is the floor of the tile's south edge.
    The longitude in the name is the floor of the tile's west edge.
    """
    lat_min = math.floor(south)
    lat_max = math.floor(north)  # floor because tile covers [lat, lat+1)
    lon_min = math.floor(west)
    lon_max = math.floor(east)

    tiles = []
    for lat in range(lat_min, lat_max + 1):
        for lon in range(lon_min, lon_max + 1):
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            name = f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"
            tiles.append(name)

    return tiles


def download_glo30(
    geojson_path: str | Path,
    output_dem: str | Path,
    target_epsg: str = "EPSG:32635",
    pixel_size_m: float = 30.0,
) -> Path:
    """Download Copernicus GLO-30 DEM tiles, mosaic, reproject, clip to basin.

    Args:
        geojson_path: Basin boundary GeoJSON.
        output_dem: Output DEM GeoTIFF path.
        target_epsg: Target CRS (UTM recommended for WhiteboxTools).
        pixel_size_m: Output pixel size in meters.

    Returns:
        Path to output DEM GeoTIFF.
    """
    geojson_path = Path(geojson_path)
    output_dem = Path(output_dem)
    output_dem.parent.mkdir(parents=True, exist_ok=True)

    bbox = basin_bbox(geojson_path)
    log.info("Basin bbox: W=%.2f S=%.2f E=%.2f N=%.2f", *bbox)

    # Add small buffer (0.05°) to ensure coverage
    buffered = (bbox[0] - 0.05, bbox[1] - 0.05, bbox[2] + 0.05, bbox[3] + 0.05)
    tiles = glo30_tile_names(*buffered)
    log.info("GLO-30 tiles needed: %d — %s", len(tiles), tiles)

    # Download tiles
    tmp_dir = output_dem.parent / "_glo30_tmp"
    tmp_dir.mkdir(exist_ok=True)

    tile_paths = []
    for tile_name in tiles:
        url = f"{GLO30_BASE_URL}/{tile_name}/{tile_name}.tif"
        local_path = tmp_dir / f"{tile_name}.tif"

        if local_path.exists():
            log.info("  Tile cached: %s", tile_name)
        else:
            log.info("  Downloading: %s", tile_name)
            try:
                with urllib.request.urlopen(url, timeout=120) as resp, open(local_path, "wb") as fh:
                    shutil.copyfileobj(resp, fh, 1 << 20)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    log.warning("  Tile not found (ocean?): %s — skipping", tile_name)
                    continue
                raise

        tile_paths.append(local_path)

    if not tile_paths:
        raise RuntimeError("No GLO-30 tiles downloaded — check basin bbox")

    # Mosaic as a VRT (no pixels in Python memory) — gdalwarp streams it in chunks.
    # rasterio.merge held every tile in RAM: ~577 MiB for 4 tiles, ~2.3 GB for a 2x2° region.
    mosaic_path = tmp_dir / "mosaic.vrt"
    subprocess.run(["gdalbuildvrt", "-q", str(mosaic_path), *[str(p) for p in tile_paths]],
                   check=True, capture_output=True, text=True)
    log.info("  Mosaic VRT of %d tiles → %s", len(tile_paths), mosaic_path.name)

    # Reproject to target CRS + clip to basin bbox + resample to target pixel size
    cmd = [
        "gdalwarp",
        "-overwrite",
        "-wm", os.environ.get("GDAL_WARP_MEMORY_MB", "256"),
        "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES",
        "-t_srs", target_epsg,
        "-tr", str(pixel_size_m), str(pixel_size_m),
        "-r", "bilinear",
        "-cutline", str(geojson_path),
        "-crop_to_cutline",
        "-dstnodata", "-9999",
        str(mosaic_path),
        str(output_dem),
    ]

    log.info("  Reprojecting to %s at %.0fm …", target_epsg, pixel_size_m)
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    # Cleanup tmp tiles (keep output)
    shutil.rmtree(tmp_dir, ignore_errors=True)

    # Verify
    with rasterio.open(output_dem) as src:
        st = src.stats(indexes=[1], approx=True)[0]
        log.info("  GLO-30 DEM: %dx%d, elev range %.0f–%.0f m",
                 src.width, src.height, st.min, st.max)

    return output_dem


# ---------------------------------------------------------------------------
# Legacy: extract DEM from SNAP TC (fallback)
# ---------------------------------------------------------------------------

def extract_dem_from_snap_tc(tc_tif: str | Path, output_dem: str | Path) -> Path:
    """Extract single-band elevation GeoTIFF from SNAP TC output.

    SNAP TC with saveDEM=true writes:
      band 1 = Sigma0_VV
      band 2 = elevation

    Writes a single-band float32 GeoTIFF.
    Fallback for when GLO-30 is not available.
    """
    tc_tif = Path(tc_tif)
    output_dem = Path(output_dem)

    with rasterio.open(tc_tif) as src:
        elev_idx = None
        for i in range(src.count):
            desc = (src.descriptions[i] or "").lower()
            if "elevation" in desc or "dem" in desc:
                elev_idx = i + 1
                break

        if elev_idx is None:
            if src.count >= 2:
                elev_idx = 2
                log.info("No elevation band description, using band 2")
            else:
                raise ValueError(
                    f"Cannot find elevation band in {tc_tif.name} "
                    f"({src.count} bands: {src.descriptions})"
                )

        elevation = src.read(elev_idx).astype(np.float32)
        profile = src.profile.copy()

    profile.update(count=1, dtype="float32", nodata=-9999)
    elevation[elevation <= 0] = -9999

    with rasterio.open(output_dem, "w", **profile) as dst:
        dst.write(elevation, 1)

    log.info("Extracted DEM band %d → %s (%dx%d)",
             elev_idx, output_dem.name, elevation.shape[1], elevation.shape[0])
    return output_dem


# ---------------------------------------------------------------------------
# Grid alignment
# ---------------------------------------------------------------------------

def warp_to_match(
    src_tif: str | Path,
    ref_tif: str | Path,
    output_tif: str | Path,
    resampling: str = "bilinear",
) -> Path:
    """Warp src_tif to match ref_tif grid (CRS, extent, resolution).

    Uses gdalwarp with -te, -tr, -t_srs from ref_tif.
    """
    src_tif = Path(src_tif)
    ref_tif = Path(ref_tif)
    output_tif = Path(output_tif)

    with rasterio.open(ref_tif) as ref:
        bounds = ref.bounds
        res_x = ref.transform.a
        res_y = -ref.transform.e
        crs = ref.crs.to_string()

    cmd = [
        "gdalwarp",
        "-overwrite",
        "-t_srs", crs,
        "-te", str(bounds.left), str(bounds.bottom),
              str(bounds.right), str(bounds.top),
        "-tr", str(abs(res_x)), str(abs(res_y)),
        "-r", resampling,
        str(src_tif),
        str(output_tif),
    ]

    log.info("gdalwarp %s → %s (match %s)", src_tif.name, output_tif.name, ref_tif.name)
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    return output_tif
