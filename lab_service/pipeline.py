"""Per-click basin on the precomputed national terrain of one DEM (no breach / D8 / HAND work here).

snap the outlet to the nearest stream cell → WhiteboxTools `watershed` on the district D8 pointer with a raster
pour point → polygonise → clip HAND / DEM / streams → statistics and flood extents per level.
"""
from __future__ import annotations

import math
import shutil
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.features import shapes
from rasterio.windows import Window
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform, unary_union

from lab_service import settings

EPSG = "EPSG:3035"


class LabError(Exception):
    """A user-facing failure (bad outlet, basin too large): reported as a failed job with this message."""


def _write(path: Path, arr: np.ndarray, transform, crs, nodata, dtype: str) -> Path:
    prof = dict(driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1, dtype=dtype, crs=crs, transform=transform,
                nodata=nodata, compress="deflate", tiled=True, BIGTIFF="YES")
    with rasterio.open(path, "w", **prof) as d:
        d.write(np.ma.filled(arr, nodata).astype(dtype), 1)
    return path


def snap_outlet(streams_path: Path, lon: float, lat: float, snap_max_m: float) -> dict:
    """Nearest stream cell within snap_max_m of the outlet (row, col, snapped lon/lat, distance)."""
    with rasterio.open(streams_path) as s:
        fwd = Transformer.from_crs("EPSG:4326", s.crs, always_xy=True)
        x, y = fwd.transform(lon, lat)
        row, col = s.index(x, y)
        H, W = s.height, s.width
        res = abs(s.transform.a)
        if not (0 <= row < H and 0 <= col < W):
            raise LabError("the outlet lies outside the district raster")
        rc = int(math.ceil(snap_max_m / res))
        win = Window(max(col - rc, 0), max(row - rc, 0), min(col + rc + 1, W) - max(col - rc, 0), min(row + rc + 1, H) - max(row - rc, 0))
        st = s.read(1, window=win, masked=True).filled(0)
        r0, c0 = int(win.row_off), int(win.col_off)
        rr, cc = np.nonzero(st == 1)
        if rr.size == 0:
            raise LabError(f"no stream cell within {snap_max_m:.0f} m of the outlet: move the point onto a blue line")
        dist = np.hypot(rr + r0 - row, cc + c0 - col) * res
        k = int(np.argmin(dist))
        if dist[k] > snap_max_m:
            raise LabError(f"the nearest stream cell is {dist[k]:.0f} m away (limit {snap_max_m:.0f} m): move the point onto a blue line")
        srow, scol = int(rr[k] + r0), int(cc[k] + c0)
        sx, sy = rasterio.transform.xy(s.transform, srow, scol)
        inv = Transformer.from_crs(s.crs, "EPSG:4326", always_xy=True)
        slon, slat = inv.transform(sx, sy)
        return {"row": srow, "col": scol, "lon": round(slon, 6), "lat": round(slat, 6), "snap_distance_m": round(float(dist[k]), 1),
                "shape": (H, W), "transform": s.transform, "crs": s.crs, "res": res}


def watershed(wbt, d8_path: Path, snap: dict, tmp: Path) -> np.ndarray:
    """Boolean mask of the basin over the whole district grid (WBT watershed with a one-cell pour raster)."""
    H, W = snap["shape"]
    pour = np.full((H, W), -32768, dtype=np.int16)
    pour[snap["row"], snap["col"]] = 1
    pour_path = _write(tmp / "pour.tif", pour, snap["transform"], snap["crs"], -32768, "int16")
    del pour
    ws_path = tmp / "watershed.tif"
    wbt.set_working_dir(str(tmp))
    if wbt.watershed(str(d8_path), str(pour_path), str(ws_path)) != 0:
        raise RuntimeError("WhiteboxTools watershed failed")
    with rasterio.open(ws_path) as r:
        ws = r.read(1, masked=True)
    mask = (~np.ma.getmaskarray(ws)) & (ws.filled(0) == 1)
    pour_path.unlink(missing_ok=True)
    ws_path.unlink(missing_ok=True)
    return mask


def polygonise(mask: np.ndarray, transform, crs, simplify_m: float = 0.0):
    geoms = [shape(g) for g, v in shapes(mask.astype(np.uint8), mask=mask, transform=transform) if v == 1]
    if not geoms:
        raise LabError("the watershed could not be polygonised")
    geom = unary_union(geoms).buffer(0)
    if simplify_m > 0:
        geom = geom.simplify(simplify_m, preserve_topology=True)
    return geom


def to_wgs84(geom, crs):
    tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    return shp_transform(tr.transform, geom)


def round_geojson(geom, digits: int = 6) -> dict:
    def rnd(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            return [round(float(v), digits) for v in c]
        return [rnd(x) for x in c]
    g = mapping(geom)
    return {"type": g["type"], "coordinates": rnd(g["coordinates"])}


def run_one(wbt, dem: str, paths: dict[str, Path], lon: float, lat: float, tmp: Path, progress=None) -> dict:
    """Basin + statistics for one DEM. Returns {basin_3035 (shapely), basin (GeoJSON 4326), basin_simplified, snapped_outlet, stats}."""
    tmp.mkdir(parents=True, exist_ok=True)
    snap = snap_outlet(paths["streams"], lon, lat, settings.SNAP_DISTANCE_M)
    if progress:
        progress("watershed")
    mask_full = watershed(wbt, paths["d8"], snap, tmp)
    n_cells = int(mask_full.sum())
    res = snap["res"]
    area_km2 = n_cells * res * res / 1e6
    if n_cells < 4:
        raise LabError("the watershed is smaller than 4 cells: the outlet is not on a channel")
    if area_km2 > settings.MAX_BASIN_KM2:
        raise LabError(f"basin of {area_km2:,.0f} km² is too large for the Lab (limit {settings.MAX_BASIN_KM2:,.0f} km²): use the gauge basins")
    H, W = mask_full.shape
    rows, cols = np.nonzero(mask_full)
    rmin, rmax = int(rows.min()), int(rows.max()) + 1
    cmin, cmax = int(cols.min()), int(cols.max()) + 1
    edge_touch = bool(rmin == 0 or cmin == 0 or rmax == H or cmax == W)
    win = Window(cmin, rmin, cmax - cmin, rmax - rmin)
    mask = mask_full[rmin:rmax, cmin:cmax]
    del mask_full
    transform = rasterio.windows.transform(win, snap["transform"])
    if progress:
        progress("statistics")

    def clip(path: Path):
        with rasterio.open(path) as r:
            a = r.read(1, window=win, masked=True)
        return np.ma.array(a, mask=np.ma.getmaskarray(a) | ~mask)

    hand = clip(paths["hand"])
    hand = np.ma.masked_less(hand, 0)
    dem_c = clip(paths["dem"])
    streams = clip(paths["streams"]).filled(0) == 1
    orow, ocol = snap["row"] - rmin, snap["col"] - cmin
    if not edge_touch:
        with rasterio.open(paths["dem"]) as r:
            nodata_ring = r.read(1, window=win, masked=True).mask
        from scipy import ndimage
        edge_touch = bool((ndimage.binary_dilation(mask) & ~mask & nodata_ring).any())

    elev = dem_c.compressed().astype(np.float64)
    stats = {"area_km2": round(area_km2, 3), "cells": n_cells, "snap_distance_m": snap["snap_distance_m"]}
    if elev.size:
        stats.update({"elev_min_m": int(elev.min()), "elev_max_m": int(elev.max()), "mean_elev_m": round(float(elev.mean()), 1),
                      "relief_m": int(elev.max() - elev.min())})
        q = np.quantile(elev, np.linspace(0, 1, 21))
        stats["hypsometry"] = [{"rel_area": round(1 - i / 20, 2), "rel_elev": round(float((q[i] - q[0]) / max(q[-1] - q[0], 1)), 3)} for i in range(21)]
    hv = hand.compressed().astype(np.float64)
    if hv.size:
        stats["hand_at_outlet_m"] = round(float(hand[orow, ocol]), 2) if not hand.mask[orow, ocol] else 0.0
        stats["mean_hand_m"] = round(float(hv.mean()), 2)
        hist = np.histogram(np.clip(hv, 0, 20), bins=np.arange(0, 21))[0]
        stats["hand_hist"] = [{"bin_m": i + 1, "share": round(float(hist[i] / hv.size), 4)} for i in range(20)]
        stats["flood_levels"] = [{"level_m": L, "area_km2": round(float((hv <= L).sum() * res * res / 1e6), 3),
                                  "share": round(float((hv <= L).mean()), 4)} for L in settings.LEVELS_M]
    stream_cells = int(streams.sum())
    stats["stream_cells"] = stream_cells
    stats["stream_length_km"] = round(stream_cells * res / 1000, 2)
    stats["drainage_density_km_km2"] = round(stream_cells * res / 1000 / area_km2, 3) if area_km2 else None
    # longest flow path inside the basin (WBT on the clipped pointer), as in dem_worker/basin_delineation.py
    try:
        with rasterio.open(paths["d8"]) as r:
            d8 = r.read(1, window=win)
        d8_clip = _write(tmp / "d8_clip.tif", np.where(mask, d8, 0).astype("float32"), transform, snap["crs"], 0, "float32")
        ws_clip = _write(tmp / "ws_clip.tif", np.where(mask, 1, -32768).astype("int16"), transform, snap["crs"], -32768, "int16")
        dfl = tmp / "dfl.tif"
        if wbt.downslope_flowpath_length(str(d8_clip), str(dfl), watersheds=str(ws_clip)) == 0:
            with rasterio.open(dfl) as r:
                L = r.read(1, masked=True)
            L = np.ma.array(L, mask=np.ma.getmaskarray(L) | ~mask)
            stats["main_channel_km"] = round(float(L.max()) / 1000, 2) if L.count() else None
    except Exception as e:  # noqa: BLE001 — the flow path is a nicety, never a failure
        stats["main_channel_km"] = None
        stats.setdefault("warnings", []).append(f"flowpath length unavailable: {type(e).__name__}")
    stats["quality"] = {"edge_touch": edge_touch, "warnings": stats.pop("warnings", []) +
                        (["the basin touches the district raster edge: it may be truncated"] if edge_touch else [])}
    basin_3035 = polygonise(mask, transform, snap["crs"], simplify_m=res / 2)
    basin_4326 = to_wgs84(basin_3035, snap["crs"])
    simplified = to_wgs84(polygonise(mask, transform, snap["crs"], simplify_m=45.0), snap["crs"])
    return {"dem": dem, "basin_3035": basin_3035, "basin": round_geojson(basin_4326), "basin_simplified": round_geojson(simplified),
            "snapped_outlet": {"type": "Point", "coordinates": [snap["lon"], snap["lat"]]}, "stats": stats}


def compare(a: dict, b: dict) -> dict:
    """FABDEM (a) vs Copernicus (b): IoU and area ratio in EPSG:3035, flood areas per level side by side."""
    ga, gb = a["basin_3035"], b["basin_3035"]
    union = ga.union(gb).area
    iou = round(float(ga.intersection(gb).area / union), 4) if union > 0 else None
    fa = {x["level_m"]: x["area_km2"] for x in a["stats"].get("flood_levels", [])}
    fb = {x["level_m"]: x["area_km2"] for x in b["stats"].get("flood_levels", [])}
    return {"iou": iou,
            "area_ratio_fab_to_cop": round(a["stats"]["area_km2"] / b["stats"]["area_km2"], 4) if b["stats"]["area_km2"] else None,
            "relief_diff_m": (a["stats"].get("relief_m") or 0) - (b["stats"].get("relief_m") or 0),
            "flood_area_diff_km2": [{"level_m": L, a["dem"]: fa.get(L), b["dem"]: fb.get(L),
                                     "diff_km2": round(fa[L] - fb[L], 3) if L in fa and L in fb else None} for L in settings.LEVELS_M],
            "secondary_basin": b["basin_simplified"]}


def cleanup(tmp: Path) -> None:
    shutil.rmtree(tmp, ignore_errors=True)
