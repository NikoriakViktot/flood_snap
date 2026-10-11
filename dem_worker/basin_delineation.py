"""
Per-outlet basin delineation on a cached Terrain → reproducible basin passport.

Steps (all in the terrain's projected CRS, vectors exported to EPSG:4326):
  1. snap the requested outlet to the nearest stream cell (Jenson-style, not max-accumulation)
  2. watershed (WhiteboxTools, D8 pointer, raster pour point)
  3. clip DEM / HAND / slope / accumulation / streams to the basin, Strahler order, subbasins
  4. basin statistics (area from the actual pixel size, relief, slopes, longest flowpath …)
  5. HAND inundation scenarios per level: depth = L − HAND for hydrologically connected cells
  6. COGs in EPSG:3857 for Terracotta, GeoJSON vectors, manifest.json with checksums

Output: /data/products/{basin_id}/
    manifest.json
    analysis/*.tif        projected-CRS rasters for modelling
    tiles/*.tif           EPSG:3857 COGs for Terracotta
    vectors/*.geojson     basin, streams, subbasins, longest flowpath, flood extents
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import shutil
import subprocess
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio import features
from rasterio.transform import Affine
from rasterio.windows import Window
from scipy import ndimage
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform, unary_union
from pyproj import Transformer

from terrain import Terrain

log = logging.getLogger("basin")

METHOD_VERSION = "basin.delineation.v1 / wbt-d8-hand-v1"
HAND_DISCLAIMER = (
    "hand_inundation_scenario: relative water-level scenario (depth = level − HAND) for cells below "
    "the slope limit and hydrologically connected to the drainage network. Not a hydrodynamic flood prediction: "
    "no discharge, wave propagation, roughness or travel time are considered."
)


class DelineationError(RuntimeError):
    """User-facing failure (bad outlet, no stream nearby …)."""


# ---------------------------------------------------------------------------
# raster helpers
# ---------------------------------------------------------------------------

def _read(path: Path, window: Window | None = None) -> tuple[np.ma.MaskedArray, dict]:
    with rasterio.open(path) as src:
        arr = src.read(1, masked=True, window=window)
        prof = src.profile.copy()
        if window is not None:
            prof["transform"] = src.window_transform(window)
            prof["height"], prof["width"] = arr.shape
    return arr, prof


def _write(path: Path, arr: np.ndarray, transform: Affine, crs, nodata, dtype: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.asarray(arr)
    if np.ma.isMaskedArray(arr):
        data = arr.filled(nodata)
    with rasterio.open(
        path, "w", driver="GTiff", height=data.shape[0], width=data.shape[1], count=1,
        dtype=dtype, crs=crs, transform=transform, nodata=nodata,
        compress="deflate", tiled=True, blockxsize=256, blockysize=256,
    ) as dst:
        dst.write(data.astype(dtype), 1)
    return path


def _cog_3857(src: Path, dst: Path, resampling: str) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "gdalwarp", "-overwrite", "-q", "-t_srs", "EPSG:3857", "-r", resampling,
        "-of", "COG", "-co", "COMPRESS=DEFLATE", "-co", "BLOCKSIZE=512",
        "-co", f"OVERVIEW_RESAMPLING={'NEAREST' if resampling == 'near' else 'AVERAGE'}",
        str(src), str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gdalwarp COG failed for {src.name}: {proc.stderr.strip()[-500:]}")
    return dst


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _wbt(name: str, ret: int) -> None:
    if ret != 0:
        raise RuntimeError(f"WhiteboxTools {name} failed (code {ret})")


# ---------------------------------------------------------------------------
# vector helpers
# ---------------------------------------------------------------------------

def _polygonize(mask: np.ndarray, transform: Affine, simplify_m: float, min_area_m2: float = 0.0):
    """Boolean mask → shapely (Multi)Polygon in the raster CRS."""
    geoms = [shape(g) for g, v in features.shapes(mask.astype(np.uint8), mask=mask, transform=transform) if v == 1]
    if not geoms:
        return None
    geom = unary_union(geoms)
    if min_area_m2 > 0:
        parts = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
        parts = [p for p in parts if p.area >= min_area_m2]
        if not parts:
            return None
        geom = unary_union(parts)
    return geom.simplify(simplify_m, preserve_topology=True)


def _to_wgs84(geom, epsg: str):
    tr = Transformer.from_crs(epsg, "EPSG:4326", always_xy=True)
    return shp_transform(tr.transform, geom)


def _round_geom(geojson: dict, digits: int = 6) -> dict:
    def rnd(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            return [round(float(c[0]), digits), round(float(c[1]), digits)]
        return [rnd(x) for x in c]
    return {"type": geojson["type"], "coordinates": rnd(geojson["coordinates"])}


def _read_shapefile(path: Path) -> list[dict]:
    import fiona
    with fiona.open(path) as src:
        return [{"geometry": dict(f["geometry"]), "properties": dict(f["properties"])} for f in src]


def _write_geojson(path: Path, obj: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":")))
    return path


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def delineate(wbt, terrain: Terrain, params: dict, out_dir: Path, progress=None) -> dict:
    """Run the per-outlet pipeline. `params` is the StartBasin command payload."""
    basin_id = params["basin_id"]
    threshold = int(params["stream_threshold"])
    levels = sorted(int(l) for l in params["flood_levels_m"])
    snap_max = float(params["snap_distance_m"])
    dem_src = terrain.dem_source
    epsg = terrain.epsg

    if out_dir.exists():
        shutil.rmtree(out_dir)
    tmp = out_dir / "_tmp"
    for d in ("analysis", "tiles", "vectors"):
        (out_dir / d).mkdir(parents=True, exist_ok=True)
    tmp.mkdir()
    wbt.set_working_dir(str(tmp))

    def step(pct, stage):
        log.info("[%s] %3d%% %s", basin_id, pct, stage)
        if progress:
            progress(pct, stage)

    # -- 1. outlet → cell, snap to nearest stream cell ----------------------------------------
    step(45, "snapping outlet")
    streams_path = terrain.streams(wbt, threshold)
    hand_path = terrain.hand(wbt, threshold)

    with rasterio.open(terrain.acc) as acc_src:
        tr_fwd = Transformer.from_crs("EPSG:4326", epsg, always_xy=True)
        x, y = tr_fwd.transform(params["outlet_lon"], params["outlet_lat"])
        row, col = acc_src.index(x, y)
        H, W = acc_src.height, acc_src.width
        res = abs(acc_src.transform.a)
        px_area = abs(acc_src.transform.a * acc_src.transform.e)
        if not (0 <= row < H and 0 <= col < W):
            raise DelineationError("Outlet lies outside the processed DEM extent")
        rc = int(math.ceil(snap_max / res))
        win = Window(max(col - rc, 0), max(row - rc, 0),
                     min(col + rc + 1, W) - max(col - rc, 0), min(row + rc + 1, H) - max(row - rc, 0))
        acc_win = acc_src.read(1, masked=True, window=win)
        acc_full_transform = acc_src.transform
        crs = acc_src.crs
    with rasterio.open(streams_path) as s:
        str_win = s.read(1, masked=True, window=win).filled(0)

    r0, c0 = int(win.row_off), int(win.col_off)
    rr, cc = np.nonzero(str_win == 1)
    if rr.size == 0:
        raise DelineationError(
            f"No stream cell (threshold {threshold} cells) within {snap_max:.0f} m of the outlet — "
            "move the outlet closer to a river or lower the stream threshold")
    dist = np.hypot((rr + r0 - row), (cc + c0 - col)) * res
    inside = dist <= snap_max
    if not inside.any():
        raise DelineationError(f"Nearest stream cell is {dist.min():.0f} m away (> snap distance {snap_max:.0f} m)")
    rr, cc, dist = rr[inside], cc[inside], dist[inside]
    k = int(np.argmin(dist))
    srow, scol = int(rr[k] + r0), int(cc[k] + c0)
    snap_dist = float(dist[k])
    acc_req = float(acc_win[row - r0, col - c0]) if not acc_win.mask[row - r0, col - c0] else None
    acc_snap = float(acc_win[rr[k], cc[k]])
    acc_candidates = acc_win.filled(0)[rr, cc]
    snap_warning = None
    if acc_candidates.max() > 3 * acc_snap:
        snap_warning = (f"A larger channel (flow accumulation {int(acc_candidates.max())} cells vs "
                        f"{int(acc_snap)} at the snapped cell) lies within the snap radius — verify the outlet")
    sx, sy = rasterio.transform.xy(acc_full_transform, srow, scol)
    tr_inv = Transformer.from_crs(epsg, "EPSG:4326", always_xy=True)
    snapped_lonlat = [round(v, 6) for v in tr_inv.transform(sx, sy)]

    # -- 2. watershed --------------------------------------------------------------------------
    step(50, "watershed")
    pour = np.full((H, W), -32768, dtype=np.int16)
    pour[srow, scol] = 1
    pour_path = _write(tmp / "pour.tif", pour, acc_full_transform, crs, -32768, "int16")
    ws_path = tmp / "watershed.tif"
    _wbt("watershed", wbt.watershed(str(terrain.d8), str(pour_path), str(ws_path)))
    ws, _ = _read(ws_path)
    mask_full = (~ws.mask) & (ws.filled(0) == 1)
    n_cells = int(mask_full.sum())
    if n_cells < 4:
        raise DelineationError("Watershed is smaller than 4 cells — the outlet is not on a channel")

    rows, cols = np.nonzero(mask_full)
    pad = 2
    rmin, rmax = max(rows.min() - pad, 0), min(rows.max() + pad + 1, H)
    cmin, cmax = max(cols.min() - pad, 0), min(cols.max() + pad + 1, W)
    cwin = Window(cmin, rmin, cmax - cmin, rmax - rmin)
    mask = mask_full[rmin:rmax, cmin:cmax]
    orow, ocol = srow - rmin, scol - cmin

    dem, prof = _read(terrain.dem, cwin)
    ctransform = prof["transform"]
    touches_edge = bool(rows.min() == 0 or cols.min() == 0 or rows.max() == H - 1 or cols.max() == W - 1)
    if not touches_edge:
        touches_edge = bool((ndimage.binary_dilation(mask) & dem.mask).any())

    # -- 3. clip rasters, Strahler, subbasins ---------------------------------------------------
    step(60, "clipping rasters")
    hand, _ = _read(hand_path, cwin)
    slope, _ = _read(terrain.slope, cwin)
    acc, _ = _read(terrain.acc, cwin)
    d8, d8prof = _read(terrain.d8, cwin)
    fpl, _ = _read(terrain.flowpath_len, cwin)
    streams = {n: _read(terrain.streams(wbt, n), cwin)[0].filled(0) == 1 for n in {500, 1000, 2000, threshold}}

    def clip(arr):
        a = np.ma.array(arr, mask=np.ma.getmaskarray(arr) | ~mask)
        return a

    dem_c, hand_c, slope_c, acc_c = clip(dem), clip(hand), clip(slope), clip(acc)
    hand_c = np.ma.masked_less(hand_c, 0)  # WBT leaves tiny negatives on stream cells
    stream_c = {n: s & mask for n, s in streams.items()}

    d8_clip = _write(tmp / "d8_clip.tif", clip(d8), ctransform, crs, d8prof.get("nodata", -32768) or -32768, "float32")
    str_clip = _write(tmp / "streams_clip.tif", stream_c[threshold].astype(np.int16), ctransform, crs, -32768, "int16")
    ws_clip = _write(tmp / "ws_clip.tif", np.where(mask, 1, -32768).astype(np.int16), ctransform, crs, -32768, "int16")
    dem_clip = _write(tmp / "dem_clip.tif", dem_c, ctransform, crs, -9999, "float32")

    strahler_path = tmp / "strahler.tif"
    _wbt("strahler_stream_order", wbt.strahler_stream_order(str(d8_clip), str(str_clip), str(strahler_path), zero_background=True))
    strahler, _ = _read(strahler_path)
    strahler = np.ma.masked_where(~stream_c[threshold] | (strahler.filled(0) <= 0), strahler)

    subbasins_path = tmp / "subbasins.tif"
    subbasins_ok = wbt.subbasins(str(d8_clip), str(str_clip), str(subbasins_path)) == 0
    if not subbasins_ok:
        log.warning("subbasins failed — skipping")

    # longest flowpath: headwater cell = max downslope flowpath length inside the basin
    dfl_path = tmp / "downslope_len.tif"
    _wbt("downslope_flowpath_length", wbt.downslope_flowpath_length(str(d8_clip), str(dfl_path), watersheds=str(ws_clip)))
    dfl, _ = _read(dfl_path)
    dfl = clip(dfl)
    hw = np.unravel_index(int(np.ma.argmax(dfl)), dfl.shape)

    # -- 4. statistics --------------------------------------------------------------------------
    step(70, "statistics")
    area_m2 = n_cells * px_area
    elev = dem_c.compressed()
    outlet_elev = float(dem_c[orow, ocol]) if not dem_c.mask[orow, ocol] else float(elev.min())
    hw_elev = float(dem_c[hw]) if not dem_c.mask[hw] else float(elev.max())
    L_m = float(fpl[orow, ocol]) if not fpl.mask[orow, ocol] else float(dfl.max())
    L_m = max(L_m, float(dfl.max()) if dfl.count() else 0.0)
    path_slope = (hw_elev - outlet_elev) / L_m if L_m > 0 else None

    basin_geom = _polygonize(mask, ctransform, simplify_m=res / 2)
    if basin_geom is None:
        raise DelineationError("Could not polygonise the watershed")
    perimeter_km = basin_geom.length / 1000
    centroid = _to_wgs84(basin_geom.centroid, epsg)

    stream_order = int(strahler[orow, ocol]) if not strahler.mask[orow, ocol] else int(strahler.max() or 0)

    # stream vector (Strahler order as attribute) → drainage density
    streams_vec_path = tmp / "streams.shp"
    streams_fc = {"type": "FeatureCollection", "features": []}
    total_stream_km = 0.0
    strahler_tif = _write(tmp / "strahler_clip.tif", strahler, ctransform, crs, -32768, "int16")
    if wbt.raster_streams_to_vector(str(strahler_tif), str(d8_clip), str(streams_vec_path)) == 0:
        for f in _read_shapefile(streams_vec_path):
            g = shape(f["geometry"])
            total_stream_km += g.length / 1000
            order = f["properties"].get("STRM_VAL") or f["properties"].get("VALUE")
            streams_fc["features"].append({
                "type": "Feature",
                "geometry": _round_geom(mapping(_to_wgs84(g, epsg))),
                "properties": {"strahler_order": int(order) if order is not None else None,
                               "length_km": round(g.length / 1000, 3)},
            })
    else:
        log.warning("raster_streams_to_vector failed — streams.geojson will be empty")

    subbasins_fc = None
    if subbasins_ok:
        sb, _ = _read(subbasins_path)
        sb = clip(sb).filled(0).astype(np.int32)
        feats = []
        for g, v in features.shapes(sb, mask=sb > 0, transform=ctransform):
            feats.append((int(v), shape(g)))
        by_id: dict[int, list] = {}
        for v, g in feats:
            by_id.setdefault(v, []).append(g)
        subbasins_fc = {"type": "FeatureCollection", "features": []}
        for v, gs in sorted(by_id.items()):
            g = unary_union(gs).simplify(res / 2, preserve_topology=True)
            subbasins_fc["features"].append({
                "type": "Feature",
                "geometry": _round_geom(mapping(_to_wgs84(g, epsg))),
                "properties": {"subbasin_id": v, "area_km2": round(g.area / 1e6, 4)},
            })

    hyps_q = list(range(0, 101, 5))
    hyps = [{"elev_m": round(float(e), 1), "frac_area_above": round(1 - q / 100, 2)}
            for q, e in zip(hyps_q, np.percentile(elev, hyps_q))]

    cn_mean, lag_h, lag_method = None, None, None
    if params.get("cn_grid_tif") and Path(params["cn_grid_tif"]).exists():
        cn_mean = _zonal_mean(Path(params["cn_grid_tif"]), basin_geom, epsg)
    if cn_mean and path_slope and L_m > 0:
        # SCS lag equation, NEH Part 630 Ch. 15: Tlag[h] = L^0.8 (S+1)^0.7 / (1900 Y^0.5)
        # L in ft (longest flowpath), S = 1000/CN − 10 (in), Y = flowpath slope in percent.
        Y = max(path_slope * 100, 0.1)
        S = 1000 / cn_mean - 10
        lag_h = ((L_m * 3.28084) ** 0.8 * (S + 1) ** 0.7) / (1900 * math.sqrt(Y))
        lag_method = "SCS lag (NEH-630 ch.15); L = longest flowpath, Y = flowpath slope, CN = zonal mean CN2"

    stats = {
        # frontend BasinStats
        "area_km2": round(area_m2 / 1e6, 2),
        "relief_m": round(float(elev.max() - elev.min()), 1),
        "hand_at_outlet": round(float(hand_c[orow, ocol]) if not hand_c.mask[orow, ocol] else 0.0, 2),
        "flow_acc": int(acc_snap),
        "main_channel_km": round(L_m / 1000, 2),
        "mean_slope_deg": round(float(slope_c.mean()), 2),
        "stream_order": stream_order,
        "outlet_elev_m": round(outlet_elev, 1),
        "snap_distance_m": round(snap_dist, 1),
        # passport extras for modelling
        "perimeter_km": round(perimeter_km, 2),
        "compactness_gravelius": round(0.282 * perimeter_km / math.sqrt(area_m2 / 1e6), 3),
        "mean_elev_m": round(float(elev.mean()), 1),
        "median_slope_deg": round(float(np.ma.median(slope_c)), 2),
        "centroid_lonlat": [round(centroid.x, 5), round(centroid.y, 5)],
        "longest_flowpath_km": round(L_m / 1000, 2),
        "longest_flowpath_slope_m_per_m": round(path_slope, 5) if path_slope is not None else None,
        "headwater_elev_m": round(hw_elev, 1),
        "total_stream_length_km": round(total_stream_km, 2),
        "drainage_density_km_per_km2": round(total_stream_km / (area_m2 / 1e6), 3) if total_stream_km else None,
        "cn2_mean": round(cn_mean, 1) if cn_mean else None,
        "lag_time_h": round(lag_h, 2) if lag_h else None,
        "lag_method": lag_method,
        "n_cells": n_cells,
        "pixel_area_m2": round(px_area, 3),
    }

    # -- 5. HAND inundation scenarios ------------------------------------------------------------
    step(78, "HAND inundation scenarios")
    hand_f = hand_c.filled(np.inf)
    stream_mask = stream_c[threshold]
    max_slope = params.get("max_slope_deg")
    # Slope limit (same idea as the SAR compound mask): steep cells are not inundated. Stream cells
    # are always kept — in narrow mountain valleys the 30 m channel cells themselves exceed 8°, and
    # dropping them would remove the seeds of the connectivity check.
    slope_ok = np.ones_like(mask) if max_slope is None else (slope_c.filled(90.0) < float(max_slope)) | stream_mask
    scenarios = []
    prev_area = -1.0
    monotonic = True
    flood_products = []
    for L in levels:
        below = mask & (hand_f < L)
        wet = below & slope_ok
        labels, _ = ndimage.label(wet, structure=np.ones((3, 3)))
        keep = np.unique(labels[wet & stream_mask])
        keep = keep[keep > 0]
        connected = np.isin(labels, keep) & wet
        depth = np.where(connected, L - hand_f, np.nan).astype(np.float32)
        a_km2 = float(connected.sum() * px_area / 1e6)
        vol = float(np.nansum(depth) * px_area)
        if a_km2 < prev_area - 1e-9:
            monotonic = False
        prev_area = a_km2

        name = f"flood_{L}m"
        utm = _write(out_dir / "analysis" / f"{name}.tif", np.ma.masked_invalid(depth), ctransform, crs, -9999, "float32")
        cog = _cog_3857(utm, out_dir / "tiles" / f"{name}.tif", "bilinear")
        ext_rel = None
        geom = _polygonize(connected, ctransform, simplify_m=res / 2, min_area_m2=4 * px_area)
        if geom is not None:
            ext_rel = f"vectors/flood_extent_{L}m.geojson"
            _write_geojson(out_dir / ext_rel, {
                "type": "Feature",
                "geometry": _round_geom(mapping(_to_wgs84(geom, epsg))),
                "properties": {"level_m": L, "area_km2": round(a_km2, 3), "method": "hand_inundation_scenario"},
            })
        scenarios.append({
            "level_m": L, "area_km2": round(a_km2, 3), "volume_mln_m3": round(vol / 1e6, 4),
            "n_cells": int(connected.sum()),
            "slope_cells_removed": int((below & ~slope_ok).sum()),
            "isolated_cells_removed": int((wet & ~connected).sum()),
            "depth_cog": f"tiles/{name}.tif", "depth_tif": f"analysis/{name}.tif", "extent_geojson": ext_rel,
        })
        flood_products.append({"level_m": L, "tc_layer": f"{dem_src}_{basin_id}_hand_{threshold}_flood_{L}m",
                               "cog": f"tiles/{name}.tif"})

    # -- 6. rasters → analysis + COG tiles ----------------------------------------------------------
    step(88, "writing COGs")
    acc_max = float(acc_c.max()) if acc_c.count() else float(threshold)
    rasters = [
        # (file stem, array, dtype, nodata, resampling, kind, name, tc_category, colormap, stretch, units, extra)
        ("dem", dem_c, "float32", -9999, "bilinear",
         "dem_corrected" if dem_src == "fab_dem" else "dem_raw", f"{terrain.meta.get('label', dem_src)} elevation",
         "dem", "terrain", [round(float(elev.min())), round(float(elev.max()))], "m", {}),
        (f"hand_{threshold}", hand_c, "float32", -9999, "bilinear",
         "hand", f"HAND (stream threshold {threshold})", f"hand_{threshold}", "ylgnbu_r", [0, 60], "m", {}),
        ("slope_horn", slope_c, "float32", -9999, "bilinear",
         "dem_uncertainty", "Slope (degrees)", "slope_horn", "ylorrd", [0, 40], "degrees", {}),
        ("d8_accum", acc_c, "float32", -9999, "bilinear",
         "flow_accumulation", "D8 flow accumulation", "d8_accum", "greys_r", [threshold, round(acc_max)], "cells", {}),
        ("strahler", strahler, "int16", -32768, "near",
         "stream_order", "Strahler stream order", "strahler", "viridis", [1, max(stream_order, 1)], "order", {}),
    ]
    for n in (500, 1000, 2000):
        rasters.append((f"stream_raster_{n}", np.ma.masked_where(~stream_c[n], stream_c[n].astype(np.int16)),
                        "int16", -32768, "near", "stream_network" if n == 2000 else "stream_network_aux",
                        f"Drainage network (≥{n} cells)", f"stream_raster_{n}", "explicit", [0, 1], "binary",
                        {"explicit_color_map": urllib.parse.quote('{"1":[167,139,250]}')}))

    products = []
    for stem, arr, dtype, nodata, resamp, kind, name, cat, cmap, stretch, units, extra in rasters:
        utm = _write(out_dir / "analysis" / f"{stem}.tif", arr, ctransform, crs, nodata, dtype)
        cog = _cog_3857(utm, out_dir / "tiles" / f"{stem}.tif", resamp)
        layer = f"{dem_src}_{basin_id}_{stem}"
        p = {"kind": kind, "name": name, "tc_category": cat, "tc_layer": layer,
             "cog": f"tiles/{stem}.tif", "tif": f"analysis/{stem}.tif",
             "colormap": cmap, "stretch_range": stretch, "units": units, "crs_tiles": "EPSG:3857", **extra}
        valid = arr.compressed() if np.ma.isMaskedArray(arr) else arr[np.isfinite(arr)]
        if valid.size:
            p["stats"] = {"min": round(float(valid.min()), 2), "max": round(float(valid.max()), 2),
                          "mean": round(float(valid.mean()), 2)}
        products.append(p)

    products.append({
        "kind": "flood_depth", "name": f"HAND inundation depth · {terrain.meta.get('label', dem_src)}",
        "tc_category": "flood_scenarios",
        "tc_layer": flood_products[0]["tc_layer"],
        "tc_layer_template": f"{dem_src}_{basin_id}_hand_{threshold}_flood_{{level}}m",
        "cog": flood_products[0]["cog"], "datasets": flood_products,
        "colormap": "blues", "stretch_range": [0, 5], "units": "m", "crs_tiles": "EPSG:3857",
        "stats": {"hand_stream_threshold": threshold},
    })

    # -- vectors ---------------------------------------------------------------------------------
    step(94, "vectors")
    vectors = {}
    basin_wgs = _round_geom(mapping(_to_wgs84(basin_geom, epsg)))
    _write_geojson(out_dir / "vectors/basin.geojson", {
        "type": "Feature", "geometry": basin_wgs,
        "properties": {"basin_id": basin_id, **stats},
    })
    vectors["basin"] = "vectors/basin.geojson"
    _write_geojson(out_dir / "vectors/streams.geojson", streams_fc)
    vectors["streams"] = "vectors/streams.geojson"
    if subbasins_fc is not None:
        _write_geojson(out_dir / "vectors/subbasins.geojson", subbasins_fc)
        vectors["subbasins"] = "vectors/subbasins.geojson"
    hw_x, hw_y = rasterio.transform.xy(ctransform, int(hw[0]), int(hw[1]))
    hw_lonlat = [round(v, 6) for v in tr_inv.transform(hw_x, hw_y)]
    _write_geojson(out_dir / "vectors/outlet.geojson", {
        "type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [params["outlet_lon"], params["outlet_lat"]]},
             "properties": {"role": "requested"}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": snapped_lonlat},
             "properties": {"role": "snapped", "snap_distance_m": round(snap_dist, 1)}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": hw_lonlat},
             "properties": {"role": "headwater_of_longest_flowpath", "elev_m": round(hw_elev, 1)}},
        ]})
    vectors["outlet"] = "vectors/outlet.geojson"

    # -- manifest ---------------------------------------------------------------------------------
    hand_valid = hand_c.compressed()
    quality = {
        "touches_dem_edge": touches_edge,
        "hand_range_m": [round(float(hand_valid.min()), 2), round(float(hand_valid.max()), 2)] if hand_valid.size else None,
        "hand_percentiles_m": {f"p{q}": round(float(v), 1) for q, v in
                               zip((5, 50, 95), np.percentile(hand_valid, (5, 50, 95)))} if hand_valid.size else None,
        "flood_area_monotonic": monotonic,
        "snap_warning": snap_warning,
        "checks": [
            # HAND can never exceed the basin relief: catches the 33,776 m class of bugs (CRS/nodata).
            # A fixed 0–50 m bound only holds for lowland floodplains, not mountain basins.
            {"name": "hand_within_relief", "ok": bool(hand_valid.size and hand_valid.min() >= 0
                                                      and hand_valid.max() <= stats["relief_m"] + 1)},
            {"name": "basin_not_truncated", "ok": not touches_edge},
            {"name": "flood_area_monotonic", "ok": monotonic},
            {"name": "stream_at_outlet", "ok": bool(stream_mask[orow, ocol])},
        ],
    }
    manifest = {
        "schema": "basin.manifest.v1",
        "basin_id": basin_id,
        "run_id": params.get("run_id"),
        "project_id": params.get("project_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "method": {
            "version": METHOD_VERSION,
            "conditioning": "breach_depressions(fill_pits=True)",
            "flow_routing": "D8",
            "stream_definition": f"flow accumulation >= {threshold} cells",
            "outlet_snapping": "nearest stream cell within snap_distance_m (Jenson); flow-accumulation maximum NOT used",
            "hand": "elevation_above_stream (WhiteboxTools)",
            "slope": "Horn 3x3, degrees",
            "flood_scenarios": HAND_DISCLAIMER,
            "subbasins_note": "raw WhiteboxTools subbasins per stream link; no HEC-HMS topology/reaches",
        },
        "parameters": {
            "dem_source": dem_src, "stream_threshold": threshold, "hand_threshold_m": params["hand_threshold_m"],
            "snap_distance_m": snap_max, "flood_levels_m": levels, "max_slope_deg": max_slope, "target_epsg": epsg,
            "pixel_size_m": terrain.res, "dem_bbox": terrain.meta.get("bbox_wgs84"),
        },
        "dem": {
            "source": dem_src, "label": terrain.meta.get("label"), "version": terrain.meta.get("version"),
            "vertical_datum": terrain.meta.get("vertical_datum"), "attribution": terrain.meta.get("attribution"),
            "licence": terrain.meta.get("licence"), "processing_crs": terrain.meta.get("processing_crs"),
            "grid_resolution_m": terrain.res, "terrain_key": terrain.key, "bbox_wgs84": terrain.meta.get("bbox_wgs84"),
        },
        "software": {"whitebox": terrain.meta.get("whitebox_version"), "gdal": terrain.meta.get("gdal_version"),
                     "rasterio": rasterio.__version__},
        "outlet": {
            "requested": [params["outlet_lon"], params["outlet_lat"]],
            "snapped": snapped_lonlat,
            "snap_distance_m": round(snap_dist, 1),
            "actual_snap_distance_m": round(snap_dist, 1),
            "flow_acc_requested": int(acc_req) if acc_req is not None else None,
            "flow_acc_snapped": int(acc_snap),
            "snap_warning": snap_warning,
        },
        "stats": stats,
        "hypsometry": hyps,
        "flood_scenarios": {
            "method": "hand_inundation_scenario",
            "disclaimer": HAND_DISCLAIMER,
            "hand_stream_threshold": threshold,
            "connected_to_streams_only": True,
            "max_slope_deg": max_slope,
            "slope_rule": None if max_slope is None else
                f"cells with slope >= {max_slope}° excluded (stream cells always kept), before connectivity",
            "levels_m": levels,
            "levels": scenarios,
        },
        "products": products,
        "vectors": vectors,
        "quality": quality,
    }
    files = sorted(p for p in out_dir.rglob("*") if p.is_file() and "_tmp" not in p.parts)
    manifest["checksums"] = {str(p.relative_to(out_dir)): _sha256(p) for p in files}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    shutil.rmtree(tmp, ignore_errors=True)
    step(97, "manifest written")
    return manifest


def _zonal_mean(raster: Path, geom_proj, epsg: str) -> float | None:
    """Mean of a raster inside a polygon (polygon given in `epsg`)."""
    with rasterio.open(raster) as src:
        tr = Transformer.from_crs(epsg, src.crs, always_xy=True)
        g = shp_transform(tr.transform, geom_proj)
        from rasterio.mask import mask as rio_mask
        try:
            data, _ = rio_mask(src, [mapping(g)], crop=True, filled=False)
        except ValueError:
            return None
        vals = data[0].compressed()
        return float(vals.mean()) if vals.size else None
