"""HAND model of one delineated basin on one DEM: the owner's pipeline on a buffered window of the Float32 DEM.

snap → `watershed` on the staged district D8 (the same basin as the delineation) → window = basin bbox + buffer (the DEM
is never clipped to the basin before D8) → WhiteboxTools: breach_depressions → d8_pointer → d8_flow_accumulation →
extract_streams(threshold) → elevation_above_stream (HAND) → slope (original DEM) → stream_link_identifier → subbasins →
stream_link_length → numpy: mask to the basin, crop to its bbox, statistics, reach table (slope per link).
Products are written by packaging.py; nothing here leaves the job's _tmp."""
from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import transform_bounds
from rasterio.windows import Window

from lab_service import pipeline as P
from lab_service import settings
from lab_service.hand import reuse as RU
from lab_service.hand import src as SRC

NODATA = -9999.0
LEVELS_M = tuple(range(1, 11))
HIST_EDGES = np.arange(0, 20.5, 0.5)
STAGES = {"terrain": 5, "watershed": 15, "conditioning": 30, "flow": 45, "hand": 55, "slope": 65, "statistics": 75, "rating": 82,
          "packaging": 90}


def _wbt(call, **kwargs) -> None:
    if call(**kwargs) != 0:
        raise RuntimeError(f"WhiteboxTools {call.__name__} failed")


def _read(path: Path):
    with rasterio.open(path) as r:
        return r.read(1, masked=True)


def _ids(a: np.ma.MaskedArray) -> np.ndarray:
    return np.rint(a.filled(0)).astype(np.int32)


def _r(x, d=2):
    return round(float(x), d)


def align_catchments(catch: np.ndarray, links: np.ndarray, streams: np.ndarray):
    """Subbasins ids → the StreamLinkIdentifier id of the link each catchment drains to (both tools number the links
    in the same order, but nothing guarantees it: checked cell by cell). Returns (catchments, aligned)."""
    on = streams & (links > 0) & (catch > 0)
    if not on.any():
        return np.zeros_like(links), False
    pairs = np.unique(np.stack([catch[on], links[on]], 1), axis=0)
    _, counts = np.unique(pairs[:, 0], return_counts=True)
    if (counts > 1).any():
        return catch, False
    lut = np.zeros(int(catch.max()) + 1, dtype=np.int32)
    lut[pairs[:, 0]] = pairs[:, 1]
    return lut[catch], True


def run_hand_model(wbt, dem: str, paths: dict[str, Path], params: dict, tmp: Path, progress=None,
                   reuse: list[dict] | None = None, reuse_skipped: list[dict] | None = None) -> dict:
    """Returns the model's cropped rasters (basin bbox, NODATA outside the basin), statistics, reaches and timings.
    `reuse`: finished sub-basin models already checked by reuse.compatible (hand/reuse.py); only the part of the basin
    they do not cover is computed. `reuse_skipped`: candidates dropped before (recorded in stats.reuse)."""
    progress = progress or (lambda stage: None)
    tmp.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}
    t0 = time.time()

    def lap(name: str) -> None:
        nonlocal t0
        timings[name] = round(time.time() - t0, 2)
        t0 = time.time()

    warnings: list[str] = []
    cells = int(params["stream_threshold_cells"])
    snap = P.snap_outlet(paths["streams"], params["outlet_lon"], params["outlet_lat"], settings.SNAP_DISTANCE_M)
    progress("watershed")
    mask_full = P.watershed(wbt, paths["d8"], snap, tmp)
    res = float(snap["res"])
    n_cells = int(mask_full.sum())
    area_km2 = n_cells * res * res / 1e6
    if n_cells < 4:
        raise P.LabError("the watershed is smaller than 4 cells: the outlet is not on a channel")
    if area_km2 > settings.HAND_MAX_BASIN_KM2:
        raise P.LabError(f"basin of {area_km2:,.0f} km² exceeds the HAND model limit of {settings.HAND_MAX_BASIN_KM2:,.0f} km² "
                         "(in-browser delivery); larger basins need the tiled delivery of a later milestone")
    lap("watershed")
    H, W = mask_full.shape
    rows, cols = np.nonzero(mask_full)
    rmin, rmax = int(rows.min()), int(rows.max()) + 1
    cmin, cmax = int(cols.min()), int(cols.max()) + 1
    edge_touch = bool(rmin == 0 or cmin == 0 or rmax == H or cmax == W)
    buf = int(math.ceil(settings.HAND_WINDOW_BUFFER_M / res))
    wr0, wr1 = max(rmin - buf, 0), min(rmax + buf, H)
    wc0, wc1 = max(cmin - buf, 0), min(cmax + buf, W)
    window_clamped = bool(wr0 > rmin - buf or wc0 > cmin - buf or wr1 < rmax + buf or wc1 < cmax + buf)
    win = Window(wc0, wr0, wc1 - wc0, wr1 - wr0)
    mask_w = mask_full[wr0:wr1, wc0:wc1]
    del mask_full
    with rasterio.open(paths["dem_f32"]) as r:
        if (r.height, r.width) != (H, W) or not r.transform.almost_equals(snap["transform"], precision=1e-3):
            raise P.LabError("the staged Float32 DEM is not on the district grid of d8.tif: re-stage the district")
        dem_w = r.read(1, window=win, masked=True)
        wtransform = rasterio.windows.transform(win, r.transform)
        crs = r.crs
    missing = int((np.ma.getmaskarray(dem_w) & mask_w).sum())
    if missing:
        warnings.append(f"{missing} basin cells have no DEM value (district edge): HAND is undefined there")
    dem_path = P._write(tmp / "dem.tif", dem_w, wtransform, crs, NODATA, "float32")
    sel: dict = {"chosen": [], "skipped": list(reuse_skipped or [])}
    d8_w = None
    if reuse:
        with rasterio.open(paths["d8"]) as r:
            d8_w = r.read(1, window=win).astype(np.int16)
        s = RU.select(wbt, reuse, d8_w, wtransform, crs, mask_w, tmp, P._write)
        sel = {"chosen": s["chosen"], "skipped": sel["skipped"] + s["skipped"]}
    covered = np.zeros_like(mask_w)
    stub = np.zeros_like(mask_w)
    for ch in sel["chosen"]:
        covered |= ch["mask"]
        stub[ch["stub"]] = True
    src_path = dem_path
    if sel["chosen"]:
        # only the uncovered part (+ one stub cell per sub-basin outlet) is conditioned and routed
        src_path = P._write(tmp / "dem_residual.tif", np.ma.array(dem_w, mask=np.ma.getmaskarray(dem_w) | (covered & ~stub)),
                            wtransform, crs, NODATA, "float32")
    lap("terrain")

    progress("conditioning")
    wbt.set_working_dir(str(tmp))
    f = {n: tmp / f"{n}.tif" for n in ("breached", "d8", "acc", "streams", "hand", "slope", "links", "catch", "llen")}
    _wbt(wbt.breach_depressions, dem=str(src_path), output=str(f["breached"]), fill_pits=True, flat_increment=0.001)
    lap("conditioning")
    progress("flow")
    _wbt(wbt.d8_pointer, dem=str(f["breached"]), output=str(f["d8"]))
    if sel["chosen"]:
        _reuse_flow(wbt, f, d8_w, stub, sel["chosen"])
    else:
        _wbt(wbt.d8_flow_accumulation, i=str(f["breached"]), output=str(f["acc"]), out_type="cells")
    _wbt(wbt.extract_streams, flow_accum=str(f["acc"]), output=str(f["streams"]), threshold=float(cells), zero_background=False)
    lap("flow")
    progress("hand")
    _wbt(wbt.elevation_above_stream, dem=str(f["breached"]), streams=str(f["streams"]), output=str(f["hand"]))
    lap("hand")
    progress("slope")
    _wbt(wbt.slope, dem=str(dem_path), output=str(f["slope"]), units="degrees")
    _wbt(wbt.stream_link_identifier, d8_pntr=str(f["d8"]), streams=str(f["streams"]), output=str(f["links"]))
    _wbt(wbt.subbasins, d8_pntr=str(f["d8"]), streams=str(f["streams"]), output=str(f["catch"]))
    _wbt(wbt.stream_link_length, d8_pntr=str(f["d8"]), linkid=str(f["links"]), output=str(f["llen"]))
    lap("slope")

    progress("statistics")
    c = (slice(rmin - wr0, rmax - wr0), slice(cmin - wc0, cmax - wc0))      # the basin bbox inside the window
    basin = mask_w[c]
    transform = rasterio.windows.transform(Window(cmin, rmin, cmax - cmin, rmax - rmin), snap["transform"])
    dem_c = dem_w[c]
    valid_dem = basin & ~np.ma.getmaskarray(dem_c)
    breached = _read(f["breached"])[c]
    hand_a = _read(f["hand"])[c]
    valid = valid_dem & ~np.ma.getmaskarray(hand_a)
    hand_raw = hand_a.filled(0).astype(np.float32)
    neg = int(((hand_raw < -1e-3) & valid).sum())
    if neg:
        warnings.append(f"{neg} cells had negative HAND and were set to 0")
    hand = np.where(valid, np.maximum(hand_raw, 0), NODATA).astype(np.float32)
    slope_a = _read(f["slope"])[c]
    slope = np.where(valid_dem & ~np.ma.getmaskarray(slope_a), slope_a.filled(0), NODATA).astype(np.float32)
    st = _read(f["streams"])[c]
    streams = basin & ~np.ma.getmaskarray(st) & (st.filled(0) == 1)
    links = np.where(basin, _ids(_read(f["links"])[c]), 0).astype(np.int32)
    catch_raw = np.where(basin, _ids(_read(f["catch"])[c]), 0).astype(np.int32)
    catchments, aligned = align_catchments(catch_raw, links, streams)
    if not aligned:
        warnings.append("catchment ids could not be matched to the stream link ids: catchments keep the Subbasins numbering")
    cond_ok = valid_dem & ~np.ma.getmaskarray(breached)
    cond = np.where(cond_ok, breached.filled(0) - dem_c.filled(0), NODATA).astype(np.float32)
    acc = _read(f["acc"])[c].filled(0)
    llen = _read(f["llen"])[c].filled(0)
    d8 = _ids(_read(f["d8"])[c])
    reuse_stats = None
    if reuse is not None or sel["skipped"]:
        cov_c = covered[c]
        links[cov_c] = 0
        streams = streams & ~cov_c
        catchments[cov_c] = 0
        reaches = _reaches(links, streams, acc, llen, d8, breached.filled(NODATA), res)
        hand, cond, cond_ok, valid, links, catchments, streams, reaches, reuse_stats = _paste(
            sel, c, transform, basin, hand, cond, cond_ok, valid, links, catchments, streams, reaches, d8_w, res,
            int(basin.sum()), warnings)
    else:
        reaches = _reaches(links, streams, acc, llen, d8, breached.filled(NODATA), res)
    if not streams.any():
        raise P.LabError(f"no stream cell inside the basin at {params['stream_threshold_km2']:g} km²: choose a smaller threshold")
    stats = _summary(basin, valid, valid_dem, hand, slope, cond, cond_ok, streams, links, dem_c, res, area_km2, n_cells, snap,
                     edge_touch, window_clamped, len(reaches), warnings)
    if reuse_stats is not None:
        stats["reuse"] = reuse_stats
    lap("statistics")
    progress("rating")
    hydrotable = SRC.rating_curves(hand, catchments if aligned else np.zeros_like(catchments), slope, reaches, res, NODATA)
    if not aligned:
        warnings.append("no rating curves: catchments are not matched to the stream links")
    lap("rating")
    hgt, wid = basin.shape
    bounds_3035 = [transform.c, transform.f + hgt * transform.e, transform.c + wid * transform.a, transform.f]
    bounds_4326 = [round(x, 6) for x in transform_bounds(crs, "EPSG:4326", *bounds_3035)]
    return {"dem": dem, "crs": crs, "transform": transform, "res": res, "shape": [hgt, wid], "basin": basin, "hand": hand,
            "slope": slope, "cond": cond, "links": links, "catchments": catchments, "streams": streams, "stats": stats,
            "reaches": {"schema": "geohydroai.reaches/v1", "crs": crs.to_string(), "res_m": res, "slope_unit": "m/m",
                        "reaches": reaches},
            "hydrotable": hydrotable,
            "window": {"rows": int(wr1 - wr0), "cols": int(wc1 - wc0), "buffer_m": settings.HAND_WINDOW_BUFFER_M,
                       "buffer_cells": buf, "clamped": window_clamped,
                       "bounds_3035": [wtransform.c, wtransform.f + (wr1 - wr0) * wtransform.e,
                                       wtransform.c + (wc1 - wc0) * wtransform.a, wtransform.f]},
            "bounds_3035": bounds_3035, "bounds_4326": bounds_4326,
            "snapped_outlet": {"type": "Point", "coordinates": [snap["lon"], snap["lat"]]},
            "timings_s": timings, "warnings": warnings}


def _summary(basin, valid, valid_dem, hand, slope, cond, cond_ok, streams, links, dem_c, res, area_km2, n_cells, snap,
             edge_touch, window_clamped, n_links, warnings) -> dict:
    hv = hand[valid].astype(np.float64)
    if hv.size == 0:
        raise P.LabError("no HAND value inside the basin")
    q = np.percentile(hv, [50, 90])
    hist = np.histogram(np.clip(hv, 0, 20), bins=HIST_EDGES)[0]
    sv = slope[valid_dem].astype(np.float64)
    sq = np.percentile(sv, [50, 90]) if sv.size else [0, 0]
    carve = np.where(cond_ok, -cond, 0).astype(np.float64)        # positive = lowered by breaching
    changed = cond_ok & (np.abs(cond) > 1e-3)
    ev = dem_c.compressed().astype(np.float64) if valid_dem.any() else np.zeros(0)
    stream_cells = int(streams.sum())
    length_km = round(stream_cells * res / 1000, 3)
    return {
        "area_km2": _r(area_km2, 3), "cells": n_cells,
        "basin": {"area_km2": _r(area_km2, 3), "cells": n_cells, "snap_distance_m": snap["snap_distance_m"],
                  "outlet_snapped": [snap["lon"], snap["lat"]], "edge_touch": edge_touch, "window_clamped": window_clamped},
        "hand": {"min": _r(hv.min()), "max": _r(hv.max()), "mean": _r(hv.mean()), "p50": _r(q[0]), "p90": _r(q[1]),
                 "hist_0_20m_step_0_5": [round(float(x) / hv.size, 4) for x in hist],
                 "share_le": {str(L): round(float((hv <= L).mean()), 4) for L in (1, 2, 3, 5)}},
        "flood_levels": [{"level_m": L, "area_km2": round(float((hv <= L).sum()) * res * res / 1e6, 3),
                          "share": round(float((hv <= L).mean()), 4)} for L in LEVELS_M],
        "slope": {"mean": _r(sv.mean() if sv.size else 0), "p50": _r(sq[0]), "p90": _r(sq[1]),
                  "share_gt": {str(d): round(float((sv > d).mean()), 4) if sv.size else 0.0 for d in (5, 10, 15, 20, 30)}},
        "streams": {"cells": stream_cells, "length_km": length_km, "links": n_links,
                    "drainage_density_km_km2": round(length_km / area_km2, 3) if area_km2 else None},
        "conditioning": {"cells_changed": int(changed.sum()), "max_carve_m": _r(max(float(carve.max()), 0.0)),
                         "mean_carve_m": _r(carve[changed & (carve > 0)].mean()) if (changed & (carve > 0)).any() else 0.0,
                         "max_raise_m": _r(max(float((-carve).max()), 0.0)),
                         "volume_m3": round(float(carve[carve > 0].sum()) * res * res, 1)},
        "dem": {"elev_min_m": _r(ev.min(), 1) if ev.size else None, "elev_max_m": _r(ev.max(), 1) if ev.size else None,
                "mean_elev_m": _r(ev.mean(), 1) if ev.size else None},
        "quality": {"edge_touch": edge_touch, "window_clamped": window_clamped, "warnings": list(warnings)},
    }


DIAGONAL = {1, 4, 16, 64}      # WhiteboxTools D8 pointer codes of the diagonal directions
# WhiteboxTools D8 pointer code → (row, col) step: 1 NE, 2 E, 4 SE, 8 S, 16 SW, 32 W, 64 NW, 128 N
D8_STEP = {1: (-1, 1), 2: (0, 1), 4: (1, 1), 8: (1, 0), 16: (1, -1), 32: (0, -1), 64: (-1, -1), 128: (-1, 0)}


def _reaches(links, streams, acc, llen, d8, breached, res) -> list[dict]:
    """One row per stream link inside the basin (per-reach stages and synthetic rating curves build on it, hand/src.py).
    `to` is the link the reach flows into (0: it leaves the basin).
    `length_m` is the link's path inside the basin (cell steps along the D8 pointer, diagonals × √2); `slope` =
    (highest − lowest breached elevation of those cells) / length_m — the breached DEM is monotone along a link, so
    the ends are its extremes. `length_link_m` is WhiteboxTools StreamLinkLength (the whole link, buffer included).
    WhiteboxTools StreamLinkSlope was not used: its values did not follow the channel gradient on a synthetic valley
    (2026-10-13)."""
    on = streams & (links > 0)
    out = []
    width = links.shape[1]
    steps = np.where(np.isin(d8, list(DIAGONAL)), res * math.sqrt(2), res)
    for i in np.unique(links[on]):
        idx = np.flatnonzero(on & (links == i))
        a = acc.ravel()[idx]
        k = int(idx[int(np.argmax(a))])
        row, col = divmod(k, width)
        z = breached.ravel()[idx]
        z = z[z != NODATA]
        length_m = float(steps.ravel()[idx].sum())
        slope = float(z.max() - z.min()) / length_m if z.size and length_m > 0 else 0.0
        dr, dc = D8_STEP.get(int(d8[row, col]), (0, 0))
        r2, c2 = row + dr, col + dc
        to = int(links[r2, c2]) if (dr or dc) and 0 <= r2 < links.shape[0] and 0 <= c2 < width and on[r2, c2] else 0
        out.append({"id": int(i), "to": to if to != int(i) else 0, "cells": int(idx.size), "length_m": round(length_m, 1),
                    "length_link_m": round(float(llen.ravel()[idx].max()), 1),
                    "slope": round(slope, 6), "slope_deg": round(math.degrees(math.atan(slope)), 4),
                    "drainage_area_km2": round(float(a.max()) * res * res / 1e6, 4),
                    "outlet_cell": [int(row), int(col)], "outlet_elev_m": round(float(breached[row, col]), 2)})
    return out


def terrain_res(paths: dict[str, Path]) -> float:
    with rasterio.open(paths["d8"]) as r:
        return float(abs(r.transform.a))


def footprint(run: dict) -> dict:
    """The cells the model covers as a GeoJSON polygon (EPSG:4326, simplified to one cell): the platform's registry of
    built models finds sub-basin models by it."""
    geom = P.polygonise(run["basin"], run["transform"], run["crs"], simplify_m=float(run["res"]))
    return P.round_geojson(P.to_wgs84(geom, run["crs"]))


def _reuse_flow(wbt, f: dict, d8_w: np.ndarray, stub: np.ndarray, chosen: list[dict]) -> None:
    """Flow accumulation of the uncovered part, the sub-basins' areas added below their stubs (hand/reuse.py)."""
    with rasterio.open(f["d8"]) as r:
        prof = r.profile
        ptr = r.read(1)
    ptr = np.where(stub, d8_w.astype(ptr.dtype), ptr)          # a stub drains where the district D8 says
    with rasterio.open(f["d8"], "w", **prof) as d:
        d.write(ptr, 1)
    _wbt(wbt.d8_flow_accumulation, i=str(f["d8"]), output=str(f["acc"]), out_type="cells", pntr=True)
    with rasterio.open(f["acc"]) as r:
        prof = r.profile
        acc = r.read(1, masked=True)
    valid = ~np.ma.getmaskarray(acc)
    nd = prof.get("nodata")
    a = acc.filled(nd if nd is not None else 0).astype(np.float64)
    RU.inject(a, ptr.astype(np.int16), valid, chosen)
    with rasterio.open(f["acc"], "w", **prof) as d:
        d.write(a.astype(prof["dtype"]), 1)


def _paste(sel, c, transform, basin, hand, cond, cond_ok, valid, links, catchments, streams, reaches, d8_w, res,
           basin_cells, warnings):
    """The sub-basins' rasters and reaches pasted into the new model (crop grid), their link ids shifted above the
    new ones; the reach that left a sub-basin flows into the new link below its stub."""
    shape = basin.shape
    next_id = max([int(links.max())] + [int(r["id"]) for r in reaches])
    used, reused_cells = [], 0
    for ch in sel["chosen"]:
        m = ch["mask"][c] & basin
        p = ch["paths"]
        h_sub, (dr, dc) = RU.read_on(p["hand"], transform, shape, NODATA, np.float32)
        cond_sub, _ = RU.read_on(p["dem_conditioning"], transform, shape, NODATA, np.float32)
        links_sub, _ = RU.read_on(p["stream_links"], transform, shape, 0, np.int32)
        catch_sub, _ = RU.read_on(p["catchments"], transform, shape, 0, np.int32)
        ids = {int(r["id"]) for r in ch["reaches"]}
        cids = set(np.unique(catch_sub[m]).tolist()) - {0}
        if not cids <= ids:
            warnings.append(f"sub-basin model {ch['hand_model_id']}: its catchments are not numbered by link, so rating "
                            "curves of its reaches may be missing")
        off = next_id
        sr, sc = ch["stub"]
        ddr, ddc = RU.D8_STEP.get(int(d8_w[sr, sc]), (0, 0))
        br, bc = sr + ddr - c[0].start, sc + ddc - c[1].start
        below = int(links[br, bc]) if (ddr or ddc) and 0 <= br < shape[0] and 0 <= bc < shape[1] else 0
        hv = m & (h_sub != NODATA)
        hand[m] = NODATA
        hand[hv] = h_sub[hv]
        valid[m] = hv[m]
        cv = m & (cond_sub != NODATA)
        cond[m] = NODATA
        cond[cv] = cond_sub[cv]
        cond_ok[m] = cv[m]
        links[m] = np.where(links_sub[m] > 0, links_sub[m] + off, 0)
        catchments[m] = np.where(catch_sub[m] > 0, catch_sub[m] + off, 0)
        streams[m] = links_sub[m] > 0
        for r in ch["reaches"]:
            row, col = r["outlet_cell"]
            reaches.append({**r, "id": int(r["id"]) + off, "to": (int(r["to"]) + off) if int(r["to"]) else below,
                            "outlet_cell": [int(row) + dr, int(col) + dc], "from_model": ch["hand_model_id"]})
        next_id = max(next_id, off + max(ids | cids | {0}))
        n = int(m.sum())
        reused_cells += n
        used.append({"hand_model_id": ch["hand_model_id"], "job_id": ch.get("job_id"), "cells": n, "area_km2": RU.km2(n, res),
                     "reaches": len(ch["reaches"]), "link_id_offset": off, "flows_into_link": below})
    stats = {"models": used, "skipped": sel["skipped"], "reused_km2": RU.km2(reused_cells, res),
             "computed_km2": RU.km2(basin_cells - reused_cells, res), "basin_km2": RU.km2(basin_cells, res),
             "rating_curves": "recomputed on the mosaic (the reused models' HAND, catchments and reaches, so the same curves)",
             "slope": "recomputed on the whole window (a local operator)"}
    return hand, cond, cond_ok, valid, links, catchments, streams, reaches, stats
