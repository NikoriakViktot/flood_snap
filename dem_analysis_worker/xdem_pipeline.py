"""
xDEM + WhiteboxTools analysis pipeline for dem.analysis.v1.

Provides:
  run_xdem_crs_conversion()  — ellipsoid + quasi-geoid outputs via xDEM
  run_terrain_attributes()   — slope, aspect, curvature, TPI, TRI, roughness via xDEM
  run_geomorphons()          — Geomorphons landform classification via WBT
  run_hydro_pipeline()       — full WBT hydro: breach, D8, TWI, HAND, streams,
                               Strahler order, subbasins, optional watershed from point
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import rasterio

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. xDEM vertical CRS conversion
# ---------------------------------------------------------------------------

def run_xdem_crs_conversion(
    dem_path: Path,
    stem: str,
    source_vcrs: str,
    geoid_tif: str,
    output_dir: Path,
) -> dict[str, str]:
    """Convert a DEM from source_vcrs to Ellipsoid and local quasi-geoid.

    Args:
        dem_path:    Input DEM (any vertical CRS).
        stem:        Filename stem used for output naming.
        source_vcrs: "EGM96" | "EGM08" | "Ellipsoid"
        geoid_tif:   Path to local quasi-geoid GeoTIFF (e.g. EGG2015).
        output_dir:  Directory for outputs.

    Returns:
        {"ellip": "<path>_ellip.tif", "orth": "<path>_orth.tif"}
    """
    import xdem

    dem = xdem.DEM(str(dem_path))

    # Step 1 → ellipsoidal heights
    if source_vcrs == "Ellipsoid":
        dem_ellip = dem
    else:
        dem_ellip = dem.set_vcrs(source_vcrs).to_vcrs("Ellipsoid")

    ellip_path = output_dir / f"{stem}_ellip.tif"
    dem_ellip.save(str(ellip_path))
    log.info("    [xDEM] ellipsoid → %s", ellip_path.name)

    # Step 2 → local quasi-geoid via provided geoid GeoTIFF
    dem_orth = dem_ellip.to_vcrs(geoid_tif)
    orth_path = output_dir / f"{stem}_orth.tif"
    dem_orth.save(str(orth_path))
    log.info("    [xDEM] quasi-geoid → %s", orth_path.name)

    return {"ellip": str(ellip_path), "orth": str(orth_path)}


# ---------------------------------------------------------------------------
# 2. Terrain attributes via xDEM
# ---------------------------------------------------------------------------

def run_terrain_attributes(
    dem_path: Path,
    output_dir: Path,
    stem: str = "dem",
) -> dict[str, str]:
    """Compute standard terrain attributes using xDEM.

    Outputs (all co-registered to input DEM):
      slope      — Horn method (degrees)
      aspect     — degrees clockwise from north
      curvature  — total curvature (1/m)
      tpi        — Topographic Position Index
      tri        — Terrain Ruggedness Index
      roughness  — roughness

    Returns:
        {"slope": path, "aspect": path, ..., "roughness": path}
    """
    import xdem

    dem = xdem.DEM(str(dem_path))
    results: dict[str, str] = {}

    attrs = {
        "slope":     lambda d: d.slope(method="Horn"),
        "aspect":    lambda d: d.aspect(),
        "curvature": lambda d: d.curvature(),
        "tpi":       lambda d: d.topographic_position_index(),
        "tri":       lambda d: d.terrain_ruggedness_index(),
        "roughness": lambda d: d.roughness(),
    }

    for name, fn in attrs.items():
        out = fn(dem)
        path = output_dir / f"{stem}_{name}.tif"
        out.save(str(path))
        results[name] = str(path)
        log.info("    [xDEM] %s → %s", name, path.name)

    return results


# ---------------------------------------------------------------------------
# 3. Geomorphons via WhiteboxTools
# ---------------------------------------------------------------------------

def run_geomorphons(
    dem_path: Path,
    output_dir: Path,
    stem: str = "dem",
    search: int = 50,
    threshold: float = 1.0,
) -> str:
    """Compute Geomorphons landform classification (10 classes) via WhiteboxTools.

    Classes: 1=flat, 2=peak, 3=ridge, 4=shoulder, 5=spur, 6=slope,
             7=hollow, 8=footslope, 9=valley, 10=pit

    Args:
        dem_path:  Input DEM (ellipsoidal heights recommended).
        search:    Search radius in cells (lookup distance).
        threshold: Flatness threshold in degrees.

    Returns:
        Path to geomorphons classification GeoTIFF.
    """
    import whitebox
    wbt = whitebox.WhiteboxTools()
    wbt.verbose = False

    out_path = output_dir / f"{stem}_geomorphons.tif"
    wbt.geomorphons(
        dem=str(dem_path),
        output=str(out_path),
        search=search,
        threshold=threshold,
        fdist=0,
        forms=True,
    )
    log.info("    [WBT] geomorphons (search=%d, thresh=%.1f°) → %s",
             search, threshold, out_path.name)
    return str(out_path)


# ---------------------------------------------------------------------------
# 4. Full hydrological pipeline via WhiteboxTools
# ---------------------------------------------------------------------------

def run_hydro_pipeline(
    dem_path: Path,
    output_dir: Path,
    stem: str = "dem",
    stream_threshold: int = 500,
    pour_point: Optional[tuple[float, float]] = None,
) -> dict[str, str]:
    """Full hydrological + geomorphometric pipeline via WhiteboxTools.

    Steps:
      1.  Breach depressions (Lindsay 2016) for hydrologically correct DEM
      2.  D8 flow direction (pointer)
      3.  D8 flow accumulation — specific contributing area (SCA) for TWI
      4.  D8 flow accumulation — cells (for stream extraction)
      5.  Slope in radians (for TWI)
      6.  TWI = wetness_index(SCA, slope_rad)
      7.  Stream extraction from flow accumulation
      8.  Strahler stream order
      9.  HAND (elevation above stream) via elevation_above_stream
      10. Subbasins
      11. Watershed from pour point (if provided)

    Args:
        pour_point: (lon, lat) in EPSG:4326. Snapped to nearest stream cell.

    Returns:
        Dict mapping output names to file paths.
    """
    import whitebox
    wbt = whitebox.WhiteboxTools()
    wbt.verbose = False

    results: dict[str, str] = {}

    # 1. Breach depressions
    breached = output_dir / f"{stem}_breached.tif"
    wbt.breach_depressions(dem=str(dem_path), output=str(breached))
    results["breached"] = str(breached)
    log.info("    [WBT] breach_depressions → %s", breached.name)

    # 2. D8 flow direction
    d8_ptr = output_dir / f"{stem}_d8ptr.tif"
    wbt.d8_pointer(dem=str(breached), output=str(d8_ptr))
    results["d8_pointer"] = str(d8_ptr)

    # 3. D8 accumulation — SCA (for TWI)
    d8_sca = output_dir / f"{stem}_d8sca.tif"
    wbt.d8_flow_accumulation(
        i=str(breached),
        output=str(d8_sca),
        out_type="specific contributing area",
    )
    results["d8_sca"] = str(d8_sca)

    # 4. D8 accumulation — cells (for stream extraction)
    d8_cells = output_dir / f"{stem}_d8cells.tif"
    wbt.d8_flow_accumulation(
        i=str(breached),
        output=str(d8_cells),
        out_type="cells",
    )
    results["d8_accumulation"] = str(d8_cells)

    # 5. Slope in radians
    slope_rad = output_dir / f"{stem}_slope_rad.tif"
    wbt.slope(dem=str(breached), output=str(slope_rad), units="radians")
    results["slope_rad"] = str(slope_rad)

    # 6. TWI
    twi = output_dir / f"{stem}_twi.tif"
    wbt.wetness_index(sca=str(d8_sca), slope=str(slope_rad), output=str(twi))
    results["twi"] = str(twi)
    log.info("    [WBT] TWI → %s", twi.name)

    # 7. Stream extraction
    streams = output_dir / f"{stem}_streams.tif"
    wbt.extract_streams(
        flow_accum=str(d8_cells),
        output=str(streams),
        threshold=stream_threshold,
    )
    results["streams"] = str(streams)
    log.info("    [WBT] streams (threshold=%d cells) → %s", stream_threshold, streams.name)

    # 8. Strahler stream order
    strahler = output_dir / f"{stem}_strahler.tif"
    wbt.strahler_stream_order(
        d8_pntr=str(d8_ptr),
        streams=str(streams),
        output=str(strahler),
    )
    results["strahler"] = str(strahler)

    # 9. HAND — elevation above nearest stream
    hand = output_dir / f"{stem}_hand.tif"
    wbt.elevation_above_stream(
        dem=str(breached),
        streams=str(streams),
        output=str(hand),
    )
    results["hand"] = str(hand)
    log.info("    [WBT] HAND → %s", hand.name)

    # 10. Subbasins
    subbasins = output_dir / f"{stem}_subbasins.tif"
    wbt.subbasins(
        d8_pntr=str(d8_ptr),
        streams=str(streams),
        output=str(subbasins),
    )
    results["subbasins"] = str(subbasins)

    # 11. Watershed from pour point
    if pour_point:
        lon, lat = pour_point
        watershed_path = _delineate_basin_from_point(
            d8_ptr=d8_ptr,
            d8_cells=d8_cells,
            output_dir=output_dir,
            stem=stem,
            lon=lon,
            lat=lat,
            wbt=wbt,
        )
        if watershed_path:
            results["watershed"] = watershed_path

    log.info("    [WBT] hydro pipeline done (%d outputs)", len(results))
    return results


def _delineate_basin_from_point(
    d8_ptr: Path,
    d8_cells: Path,
    output_dir: Path,
    stem: str,
    lon: float,
    lat: float,
    wbt,
) -> Optional[str]:
    """Delineate a watershed from a pour point using snap + watershed tools.

    The pour point (EPSG:4326) is reprojected to match the DEM CRS,
    snapped to the nearest stream cell within 500m, then used for
    watershed delineation.
    """
    try:
        import geopandas as gpd
        from shapely.geometry import Point

        # Build a single-point GeoDataFrame
        gdf = gpd.GeoDataFrame(
            [{"id": 1, "geometry": Point(lon, lat)}],
            crs="EPSG:4326",
        )

        # Reproject pour point to match DEM CRS
        with rasterio.open(str(d8_ptr)) as src:
            dem_epsg = src.crs.to_epsg()
        if dem_epsg and dem_epsg != 4326:
            gdf = gdf.to_crs(epsg=dem_epsg)

        pp_geojson = output_dir / f"{stem}_pour_point.geojson"
        gdf.to_file(str(pp_geojson), driver="GeoJSON")

        # Snap pour point to nearest stream (within 500 m / cells)
        pp_snapped = output_dir / f"{stem}_pour_point_snapped.shp"
        wbt.snap_pour_points(
            pour_pts=str(pp_geojson),
            flow_accum=str(d8_cells),
            output=str(pp_snapped),
            snap_dist=500,
        )

        # Delineate watershed
        watershed_tif = output_dir / f"{stem}_watershed.tif"
        wbt.watershed(
            d8_pntr=str(d8_ptr),
            pour_pts=str(pp_snapped),
            output=str(watershed_tif),
        )
        log.info("    [WBT] watershed from (%.4f, %.4f) → %s", lon, lat, watershed_tif.name)
        return str(watershed_tif)

    except Exception as exc:
        log.warning("    [WBT] pour-point basin delineation failed: %s", exc)
        return None
