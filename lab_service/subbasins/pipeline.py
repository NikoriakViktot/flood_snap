"""Subbasins of one delineated basin, the way QSWAT+ delineates a SWAT+ watershed (owner, 2026-10-19: «роби як робить
QSWATPlus»; offline prototype geohydroai-storms experiments/prut-network/qswat_delineate.py).

QSWAT+ (delineation.runTauDEM, QSWATTopology.setUp), on a window of the district's Float32 DEM around the basin:
  1. the river layer (Hydro river system lines) is burnt `burn_depth_m` (QSWAT+ default 50 m) into the DEM, which is
     conditioned again (breach_depressions) and routed (D8 pointer, accumulation) — so the channels follow the mapped
     rivers where the DEM alone would cut a divide or join the wrong valley (Сопівка → Лючка → Пістинка);
  2. streams = cells whose accumulation is ≥ the stream threshold; a LINK runs between two junctions (or a source and a
     junction); every link has its own SUBBASIN = the cells that drain to that link and not through an upstream link;
  3. the gauges (user outlets) are snapped onto the streams (≤ 300 m, the cell whose drainage area is closest to the
     official one) and split their link in two;
  4. topology: each subbasin drains into the subbasin of the cell just below its outlet. Areas, elevations and slopes
     come from the UNBURNED DEM; names from the river layer (majority of the link's channel cells).
The cell of a junction belongs to the link below it, so the local area around a confluence stays in the downstream
subbasin and is never lost: Σ local areas = the basin area exactly, and every total = local + Σ upstream totals.
"""
from __future__ import annotations

import math
import time
from collections import Counter
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio import features
from rasterio.windows import Window
from shapely.geometry import LineString, mapping, shape
from shapely.ops import transform as shp_transform, unary_union

from lab_service import pipeline as P
from lab_service import settings

NODATA = -9999.0
D8 = {1: (-1, 1), 2: (0, 1), 4: (1, 1), 8: (1, 0), 16: (1, -1), 32: (0, -1), 64: (-1, -1), 128: (-1, 0)}  # WhiteboxTools
STAGES = {"terrain": 10, "watershed": 15, "burn": 25, "conditioning": 35, "flow": 55, "subbasins": 70, "polygons": 85,
          "packaging": 95}
OUTLET_SEARCH_M = 1000.0     # the user's point: the channel with the largest drainage within 1 km (as the prototype)


def _wbt(call, **kwargs) -> None:
    if call(**kwargs) != 0:
        raise RuntimeError(f"WhiteboxTools {call.__name__} failed")


def downstream_index(d8: np.ndarray) -> np.ndarray:
    """Flat index of each cell's D8 receiver; -1 = no receiver (outlet, nodata or off the window). int32 (≤ 2^31 cells)."""
    h, w = d8.shape
    rows = np.arange(h, dtype=np.int32)[:, None]
    cols = np.arange(w, dtype=np.int32)[None, :]
    down = np.full(d8.shape, -1, dtype=np.int32)
    for code, (dr, dc) in D8.items():
        sel = d8 == code
        tr = np.broadcast_to(rows + dr, d8.shape)[sel]
        tc = np.broadcast_to(cols + dc, d8.shape)[sel]
        ok = (tr >= 0) & (tr < h) & (tc >= 0) & (tc < w)
        idx = np.full(tr.shape, -1, dtype=np.int32)
        idx[ok] = tr[ok] * w + tc[ok]
        down[sel] = idx
    return down.ravel()


def first_pour(down: np.ndarray, pours: np.ndarray) -> np.ndarray:
    """Each cell → the first pour point at or below it, -1 when its path reaches no pour point (pointer doubling)."""
    nxt = down.copy()
    sink = nxt < 0
    nxt[sink] = np.flatnonzero(sink)
    nxt[pours] = pours
    for _ in range(64):                          # 2^64 steps: any path
        nn = nxt[nxt]
        if np.array_equal(nn, nxt):
            break
        nxt = nn
    is_pour = np.zeros(len(nxt), bool)
    is_pour[pours] = True
    return np.where(is_pour[nxt], nxt, -1).astype(np.int32)


def strahler(ids: list[int], up: dict[int, list[int]]) -> dict[int, int]:
    order: dict[int, int] = {}

    def of(i: int) -> int:
        if i in order:
            return order[i]
        stack, seen = [i], []
        while stack:                                 # iterative post-order: deep networks never hit the recursion limit
            j = stack.pop()
            seen.append(j)
            stack.extend(u for u in up.get(j, []) if u not in order)
        for j in reversed(seen):
            ch = [order[u] for u in up.get(j, [])]
            if not ch:
                order[j] = 1
            else:
                m = max(ch)
                order[j] = m + 1 if ch.count(m) >= 2 else m
        return order[i]

    for i in ids:
        of(i)
    return order


def _round(geom, digits: int = 5) -> dict:
    def rnd(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            return [round(float(v), digits) for v in c]
        return [rnd(x) for x in c]
    g = mapping(geom)
    return {"type": g["type"], "coordinates": rnd(g["coordinates"])}


def rivers_3035(rivers: dict | None) -> list[tuple]:
    """The river layer of the request (GeoJSON lines in EPSG:4326) → [(geometry in EPSG:3035, name), …]."""
    if not rivers or not rivers.get("features"):
        return []
    tr = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True).transform
    out = []
    for f in rivers["features"]:
        g = f.get("geometry")
        if not g or g.get("type") not in ("LineString", "MultiLineString"):
            continue
        props = f.get("properties") or {}
        out.append((shp_transform(tr, shape(g)), props.get("name") or props.get("river_id")))
    return out


def run_subbasins(wbt, paths: dict[str, Path], params: dict, tmp: Path, progress=None) -> dict:
    """`paths`: the district's d8, streams and dem_f32. Returns {subbasins, streams, gauges, summary, audit, timings}."""
    progress = progress or (lambda stage: None)
    tmp.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}
    t0 = time.time()

    def lap(name: str) -> None:
        nonlocal t0
        timings[name] = round(time.time() - t0, 2)
        t0 = time.time()

    warnings: list[str] = []
    thr_km2 = float(params["stream_threshold_km2"])
    burn_m = float(params.get("burn_depth_m", settings.SUBBASIN_BURN_DEPTH_M))
    snap_m = settings.SNAP_DISTANCE_M

    # 1. the basin on the staged district D8 (the same basin as the delineation)
    progress("watershed")
    snap = P.snap_outlet(paths["streams"], params["outlet_lon"], params["outlet_lat"], snap_m)
    mask_full = P.watershed(wbt, paths["d8"], snap, tmp)
    res = float(snap["res"])
    ckm2 = res * res / 1e6
    n0 = int(mask_full.sum())
    area0 = n0 * ckm2
    if n0 < 4:
        raise P.LabError("the watershed is smaller than 4 cells: the outlet is not on a channel")
    if area0 > settings.SUBBASIN_MAX_KM2:
        raise P.LabError(f"basin of {area0:,.0f} km² exceeds the subbasin limit of {settings.SUBBASIN_MAX_KM2:,.0f} km² "
                         "(server memory): choose an outlet further upstream")
    if thr_km2 * 4 > area0:
        raise P.LabError(f"the stream threshold {thr_km2:g} km² is too large for a basin of {area0:,.1f} km²: choose a smaller one")
    H, W = mask_full.shape
    rows, cols = np.nonzero(mask_full)
    buf = int(math.ceil(settings.SUBBASIN_WINDOW_BUFFER_M / res))
    wr0, wr1 = max(int(rows.min()) - buf, 0), min(int(rows.max()) + 1 + buf, H)
    wc0, wc1 = max(int(cols.min()) - buf, 0), min(int(cols.max()) + 1 + buf, W)
    del rows, cols
    win = Window(wc0, wr0, wc1 - wc0, wr1 - wr0)
    basin0 = mask_full[wr0:wr1, wc0:wc1].copy()
    del mask_full
    h, w = basin0.shape
    with rasterio.open(paths["dem_f32"]) as r:
        if (r.height, r.width) != (H, W) or not r.transform.almost_equals(snap["transform"], precision=1e-3):
            raise P.LabError("the staged Float32 DEM is not on the district grid of d8.tif: re-stage the district")
        dem = r.read(1, window=win, masked=True)
        tf = rasterio.windows.transform(win, r.transform)
        crs = r.crs
    valid = ~np.ma.getmaskarray(dem)
    z = dem.filled(np.nan).astype(np.float32)
    del dem
    lap("terrain")

    # 2. burn the river layer and route again
    progress("burn")
    lines = rivers_3035(params.get("rivers"))
    line_id = np.zeros((h, w), np.int32)
    if lines:
        line_id = features.rasterize(((g, i + 1) for i, (g, _) in enumerate(lines)), out_shape=(h, w), transform=tf,
                                     fill=0, dtype="int32")
    burn = (line_id > 0) & valid if burn_m > 0 else np.zeros((h, w), bool)
    if burn_m > 0 and not lines:
        warnings.append("no river layer was sent: the DEM was not burnt (channels follow the DEM alone)")
    zb = np.where(valid, z - np.where(burn, burn_m, 0.0), NODATA).astype(np.float32)
    dem_path = P._write(tmp / "dem_burned.tif", zb, tf, crs, NODATA, "float32")
    del zb
    lap("burn")
    progress("conditioning")
    wbt.set_working_dir(str(tmp))
    f = {n: tmp / f"{n}.tif" for n in ("breached", "d8", "acc")}
    _wbt(wbt.breach_depressions, dem=str(dem_path), output=str(f["breached"]), fill_pits=True, flat_increment=0.001)
    dem_path.unlink(missing_ok=True)
    lap("conditioning")
    progress("flow")
    _wbt(wbt.d8_pointer, dem=str(f["breached"]), output=str(f["d8"]))
    _wbt(wbt.d8_flow_accumulation, i=str(f["breached"]), output=str(f["acc"]), out_type="cells")
    f["breached"].unlink(missing_ok=True)
    with rasterio.open(f["d8"]) as r:
        d8 = r.read(1).astype(np.int16)
    with rasterio.open(f["acc"]) as r:
        acc = r.read(1, masked=True).filled(0).astype(np.float32).ravel()
    acc[~valid.ravel()] = 0
    down = downstream_index(np.where(valid, d8, 0))
    del d8
    lap("flow")

    # 3. outlet, region, streams, junctions, gauges
    progress("subbasins")
    orow, ocol = snap["row"] - wr0, snap["col"] - wc0
    n = int(OUTLET_SEARCH_M / res)
    rr, cc = np.mgrid[max(orow - n, 0):min(orow + n + 1, h), max(ocol - n, 0):min(ocol + n + 1, w)]
    near = np.hypot(rr - orow, cc - ocol) * res <= OUTLET_SEARCH_M
    cand = (rr[near] * w + cc[near]).astype(np.int64)
    outlet = int(cand[np.argmax(acc[cand])])
    region = first_pour(down, np.array([outlet], np.int64)) >= 0
    thr = thr_km2 / ckm2
    st = (acc >= thr) & region
    sidx = np.flatnonzero(st)
    ds = down[sidx]
    has = ds >= 0
    inflow = np.bincount(ds[has][st[ds[has]]], minlength=h * w)          # stream inflows per cell
    junction = st & (inflow >= 2)
    del inflow
    link_out = set(sidx[has & junction[np.where(has, ds, 0)]].tolist())   # stream cells just above a junction
    link_out.add(outlet)

    gauges_in = params.get("gauges") or []
    to3035 = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True).transform
    gauge_cell: dict = {}
    gauge_audit: list[dict] = []
    ng = int(snap_m / res)
    for g in gauges_in:
        x, y = to3035(g["lon"], g["lat"])
        r0, k0 = rasterio.transform.rowcol(tf, x, y)
        entry = {"post_id": g["post_id"], "name": g.get("name"), "official_km2": g.get("area_km2")}
        if not (0 <= r0 < h and 0 <= k0 < w):
            gauge_audit.append({**entry, "status": "outside"})
            continue
        gr, gc = np.mgrid[max(r0 - ng, 0):min(r0 + ng + 1, h), max(k0 - ng, 0):min(k0 + ng + 1, w)]
        idx = (gr * w + gc).ravel()
        dist = np.hypot((gr.ravel() - r0) * res, (gc.ravel() - k0) * res)
        ok = (dist <= snap_m) & region[idx] & (acc[idx] * ckm2 >= 1.0)
        idx, dist = idx[ok], dist[ok]
        if not idx.size:
            gauge_audit.append({**entry, "status": "outside" if not region[min(max(r0, 0), h - 1) * w + min(max(k0, 0), w - 1)]
                                else "no_channel"})
            continue
        off = g.get("area_km2")
        if off:
            cost = np.abs(np.log(acc[idx] * ckm2 / max(float(off), 0.1))) + 0.25 * dist / snap_m
        else:
            cost = dist - acc[idx] * 1e-9
        c = int(idx[np.argmin(cost)])
        if c in gauge_cell.values():
            gauge_audit.append({**entry, "status": "duplicate_cell"})
            continue
        gauge_cell[g["post_id"]] = c
        cx, cy = rasterio.transform.xy(tf, c // w, c % w)
        gauge_audit.append({**entry, "status": "snapped", "model_km2": round(float(acc[c] * ckm2), 1),
                            "snap_m": round(float(math.hypot(cx - x, cy - y)), 0)})
    link_out |= set(gauge_cell.values())
    pours = np.array(sorted(link_out), dtype=np.int64)
    lab = first_pour(down, pours)
    inreg = lab >= 0
    pidx = {int(p): k for k, p in enumerate(pours)}
    k_of = np.full(h * w, -1, np.int32)
    k_of[inreg] = np.searchsorted(pours, lab[inreg]).astype(np.int32)
    del lab

    # 4. per-subbasin statistics from the UNBURNED DEM
    zf = z.ravel()
    gy, gx = np.gradient(z, res)
    slope = np.degrees(np.arctan(np.hypot(gx, gy))).astype(np.float32).ravel()
    del gx, gy
    kk = k_of[inreg]
    zin = np.nan_to_num(zf[inreg])
    n_cells = np.bincount(kk, minlength=len(pours))
    zsum = np.bincount(kk, weights=zin, minlength=len(pours))
    ssum = np.bincount(kk, weights=np.nan_to_num(slope[inreg]), minlength=len(pours))
    zmax = np.full(len(pours), -np.inf)
    np.maximum.at(zmax, kk, zin)
    del zin, slope, kk
    dk = np.array([k_of[down[p]] if down[p] >= 0 else -1 for p in pours], dtype=np.int64)

    lines_name = [nm for _, nm in lines]
    line_flat = line_id.ravel()
    del line_id
    link_cells: dict[int, list[int]] = {}
    names: dict[int, str | None] = {}
    for k, p in enumerate(pours):                  # trace each link up from its outlet along the larger inflow
        cells, c = [int(p)], int(p)
        while True:
            r0, c0 = divmod(c, w)
            ins = [rr_ * w + cc_ for rr_ in range(r0 - 1, r0 + 2) for cc_ in range(c0 - 1, c0 + 2)
                   if 0 <= rr_ < h and 0 <= cc_ < w and (rr_, cc_) != (r0, c0)
                   and down[rr_ * w + cc_] == c and st[rr_ * w + cc_] and k_of[rr_ * w + cc_] == k]
            if not ins:
                break
            c = max(ins, key=lambda j: acc[j])
            cells.append(c)
        link_cells[k] = cells
        hits = Counter(int(line_flat[x]) for x in cells if line_flat[x] > 0)
        top = hits.most_common(1)
        names[k] = lines_name[top[0][0] - 1] if top and top[0][1] >= 0.3 * len(cells) else None
    lap("subbasins")

    # 5. polygons, streams, topology
    progress("polygons")
    rast = np.where(inreg, k_of + 1, 0).astype(np.int32).reshape(h, w)
    polys: dict[int, list] = {}
    for geom, v in features.shapes(rast, mask=rast > 0, transform=tf):
        polys.setdefault(int(v) - 1, []).append(shape(geom))
    del rast
    to4326 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    up: dict[int, list[int]] = {}
    for k in range(len(pours)):
        if dk[k] >= 0:
            up.setdefault(int(dk[k]), []).append(k)
    order = strahler(list(range(len(pours))), up)
    # ids 1…n numbered from the outlet upstream (breadth first), so the outlet is 1 and ids grow with distance
    root = pidx[outlet]
    bfs, seen = [root], {root}
    for k in bfs:
        for u in sorted(up.get(k, []), key=lambda u: -acc[pours[u]]):
            if u not in seen:
                seen.add(u)
                bfs.append(u)
    bfs += [k for k in range(len(pours)) if k not in seen]
    sid = {k: i + 1 for i, k in enumerate(bfs)}
    depth = {root: 0}
    for k in bfs:
        for u in up.get(k, []):
            depth[u] = depth.get(k, 0) + 1
    post_at = {c: pid for pid, c in gauge_cell.items()}
    subs, streams = [], []
    for k in bfs:
        p = int(pours[k])
        cells = link_cells[k]
        x, y = rasterio.transform.xy(tf, p // w, p % w)
        lon, lat = to4326(x, y)
        zl = [zf[c] for c in cells if not np.isnan(zf[c])]
        length_km = len(cells) * res * (1 + (math.sqrt(2) - 1) * 0.4) / 1000      # ~40 % diagonal steps
        link_slope = round(float(max(zl) - min(zl)) / (length_km * 1000), 5) if len(zl) > 1 and length_km > 0 else None
        geom = unary_union(polys.get(k, [])).buffer(0)
        geom4326 = shp_transform(to4326, geom.simplify(res, preserve_topology=True)) if not geom.is_empty else None
        props = {"id": sid[k], "name": names[k], "post_id": post_at.get(p),
                 "down": sid.get(int(dk[k])) if dk[k] >= 0 else None, "up": sorted(sid[u] for u in up.get(k, [])),
                 "strahler": order[k], "depth": depth.get(k, 0),
                 "local_km2": round(float(n_cells[k] * ckm2), 3), "total_km2": round(float(acc[p] * ckm2), 3),
                 "z_mean_m": round(float(zsum[k] / max(n_cells[k], 1)), 1),
                 "z_outlet_m": round(float(zf[p]), 1) if not np.isnan(zf[p]) else None,
                 "z_max_m": round(float(zmax[k]), 1) if np.isfinite(zmax[k]) else None,
                 "slope_deg": round(float(ssum[k] / max(n_cells[k], 1)), 2),
                 "link_km": round(length_km, 2), "link_slope": link_slope,
                 "outlet": [round(lon, 5), round(lat, 5)]}
        subs.append({"type": "Feature", "properties": props, "geometry": _round(geom4326) if geom4326 else None})
        if len(cells) > 1:
            line = LineString([rasterio.transform.xy(tf, c // w, c % w) for c in reversed(cells)])
            streams.append({"type": "Feature", "properties": {"id": sid[k], "name": names[k], "strahler": order[k]},
                            "geometry": _round(shp_transform(to4326, line.simplify(res / 2)))})

    # 6. audit: closure, a single outlet, no cycles, the burn's effect on the area
    tot = {s["properties"]["id"]: s["properties"]["total_km2"] for s in subs}
    worst = 0.0
    for s in subs:
        pr = s["properties"]
        exp = pr["local_km2"] + sum(tot[u] for u in pr["up"])
        worst = max(worst, abs(pr["total_km2"] - exp) / max(pr["total_km2"], 1e-9))
    outlets = [s["properties"]["id"] for s in subs if s["properties"]["down"] is None]
    area = float(inreg.sum() * ckm2)
    overlap = float((inreg.reshape(h, w) & basin0).sum() * ckm2)
    if abs(area - area0) / area0 > 0.02:
        warnings.append(f"the burnt river layer changed the basin by {100 * (area - area0) / area0:+.1f} % "
                        f"({area0:,.1f} → {area:,.1f} km²): check the divide on the map")
    for g in gauge_audit:
        if g.get("official_km2") and g.get("model_km2"):
            g["area_ratio"] = round(g["model_km2"] / float(g["official_km2"]), 3)
            if not 0.8 <= g["area_ratio"] <= 1.25:
                warnings.append(f"gauge {g['post_id']} ({g.get('name')}): model area {g['model_km2']} km² vs official "
                                f"{g['official_km2']} km²")
    sx, sy = rasterio.transform.xy(tf, outlet // w, outlet % w)
    olon, olat = to4326(sx, sy)
    summary = {"subbasins": len(subs), "area_km2": round(area, 1), "area_unburned_km2": round(area0, 1),
               "overlap_km2": round(overlap, 1), "stream_threshold_km2": thr_km2, "burn_depth_m": burn_m,
               "river_lines": len(lines), "gauges_snapped": len(gauge_cell), "max_strahler": max(order.values()),
               "named_subbasins": sum(1 for s in subs if s["properties"]["name"]), "outlet": [round(olon, 5), round(olat, 5)],
               "link_km_total": round(sum(s["properties"]["link_km"] for s in subs), 1)}
    audit = {"closure_worst_rel": round(worst, 6), "outlets": outlets, "single_outlet": len(outlets) == 1,
             "sum_local_km2": round(sum(s["properties"]["local_km2"] for s in subs), 3),
             "window": {"rows": h, "cols": w, "buffer_m": settings.SUBBASIN_WINDOW_BUFFER_M}}
    for k in ("d8", "acc"):
        f[k].unlink(missing_ok=True)
    lap("polygons")
    return {"subbasins": {"type": "FeatureCollection", "features": subs},
            "streams": {"type": "FeatureCollection", "features": streams},
            "gauges": gauge_audit, "summary": summary, "audit": audit, "warnings": warnings, "timings": timings,
            "snapped_outlet": {"type": "Point", "coordinates": [round(olon, 6), round(olat, 6)]}}
