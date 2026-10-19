"""
Evacuation over the roads a flood leaves open (ДСНС R4, geoai-frontend-v2 docs/LABS.md §7a; owner, 2026-10-19: «не те що
укриття — евакуація та дороги і шляхи, якщо може бути затоплено»; pilot: Verkhovynska hromada and Ivano-Frankivsk oblast).

Scenario: a river flood over the whole REGION at once (a big flood in the oblast, not the per-hromada local flood of
isolation.py): a place is under water at level L when it lies within L metres above the stream it drains to (national
FABDEM HAND) AND that stream's catchment is at least A_MIN_KM2 (stream_area.py). Without the second condition every
mountain road beside a 2 km² creek was «flooded» and 92 % of the oblast's settlements were cut off at 5 m (first run,
2026-10-19). Roads are sampled every SAMPLE_M and cut by isolation.py's rule (≥ 150 m or ≥ 80 % of the way under water;
bridges never cut themselves).

Per settlement (OSM city / town / village / hamlet of the region) and level:
  in_zone          the settlement point lies within the level (HAND ≤ L)
  isolated         no passable road leads from it to the regional network (its access nodes are cut off)
  evacuation route to the nearest SAFE settlement — a town / city or a village of ≥ 2 000 people whose point is ≥ L + 3 m
                   above the stream and still reachable — on the passable roads, by travel time; with the time on dry roads
                   to the same place (the detour the flood costs), the route length and its bottleneck (the lowest
                   one-direction capacity along the route, veh/h: the state-road estimate of roads_category.csv where the
                   road has one, else a default per road class)
  rescue access    travel time from the nearest fire station (OSM amenity=fire_station of the critical sites) on the
                   passable roads, and on dry roads
Travel time: road length / speed by class (SPEED_KMH), no traffic, no turn costs — a screening, not a traffic model.

Outputs: data/s3_staging/hydro/lake/exposure/v1/evacuation/<region>/
  settlements.parquet   one row per settlement and level (the columns above), EPSG:4326 points
  routes.parquet        one LineString per settlement in the zone with a route, per level
  cut_roads.parquet     the ways cut at each level (osm_id, name, ref, kind, rank, level), LineStrings
  summary.json          per level and per hromada: settlements in the zone, cut off, with a route, median detour, …

  ~/repo/data_dnipro_h_q/.venv-linux/bin/python scripts/national/evacuation.py [--region UA26] [--buffer-km 15]
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import duckdb
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import rasterio.windows
import shapely
from pyproj import Transformer
from scipy import sparse
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree

from buildings_exposure import ADM3, LAKE
from isolation import ACCESS_M, CUT_MIN_M, CUT_SHARE, CUT_SHORT_M, settlements

HAND_COG = Path.home() / "projects/data/national/hand_ua/fab_dem_hand_cog.tif"     # decimetres, 255 = no data / > 25.4 m
AREA_DIR = Path.home() / "projects/data/national/stream_area"                      # <district>.tif, km² (stream_area.py)
SAMPLE_M = 30.0
CRIT = LAKE / "critical_sites" / "critical_sites.parquet"
CATEGORY_CSV = Path.home() / "repo/data_dnipro_h_q/data/parquet/roads/roads_category.csv"
LEVELS = (1, 2, 3, 5)
SAFE_MARGIN_M = 3.0
SAFE_VILLAGE_POP = 2000
# km/h by risk_roads rank (1 motorway/trunk … 7 service/track); an assumption, stated on the page
SPEED_KMH = {1: 90, 2: 80, 3: 70, 4: 50, 5: 40, 6: 30, 7: 20}
# veh/h in one direction when the road has no state-road estimate
CAP_DEFAULT = {1: 1800, 2: 1500, 3: 1200, 4: 900, 5: 600, 6: 400, 7: 200}
T3035 = Transformer.from_crs(4326, 3035, always_xy=True)
T3857 = Transformer.from_crs(4326, 3857, always_xy=True)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Grid:
    """a raster window held in memory and sampled by index arithmetic (millions of road points)"""
    def __init__(self, path: Path, bounds4326, to_crs: Transformer):
        self.to = to_crs
        with rasterio.open(path) as r:
            x0, y0 = to_crs.transform(bounds4326[0], bounds4326[1])
            x1, y1 = to_crs.transform(bounds4326[2], bounds4326[3])
            win = rasterio.windows.from_bounds(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), r.transform).round_offsets().round_lengths()
            win = win.intersection(rasterio.windows.Window(0, 0, r.width, r.height))
            self.a = r.read(1, window=win)
            self.tf = r.window_transform(win)
            self.nodata = r.nodata

    def at(self, lon, lat, fill):
        x, y = self.to.transform(lon, lat)
        c = np.floor((np.asarray(x) - self.tf.c) / self.tf.a).astype(np.int64)
        r = np.floor((np.asarray(y) - self.tf.f) / self.tf.e).astype(np.int64)
        ok = (r >= 0) & (r < self.a.shape[0]) & (c >= 0) & (c < self.a.shape[1])
        out = np.full(len(c), fill, dtype=float)
        out[ok] = self.a[r[ok], c[ok]]
        return out


def flood_layers(bounds):
    hand = Grid(HAND_COG, bounds, T3857)
    areas = [Grid(f, bounds, T3035) for f in sorted(AREA_DIR.glob("*.tif")) if not f.name.endswith("_acc.tif")]

    def hand_m(lon, lat):
        v = hand.at(lon, lat, 255)
        return np.where(v >= 255, np.inf, v / 10.0)

    def area_km2(lon, lat):
        out = np.full(len(lon), np.nan)
        for g in areas:
            v = g.at(lon, lat, np.nan)
            out = np.where(np.isnan(out) & np.isfinite(v), v, out)
        return out
    return hand_m, area_km2


def region_roads(region_poly):
    minx, miny, maxx, maxy = region_poly.bounds
    con = duckdb.connect()
    con.sql("INSTALL spatial; LOAD spatial;")
    lens = ", ".join(f"len_m_hand_le_{lv}m" for lv in LEVELS)
    q = (f"SELECT osm_id, kind, rank, name, ref, len_m, {lens}, pcode, ST_AsWKB(geometry) AS wkb "
         f"FROM read_parquet('{(LAKE / 'roads/roads.parquet').as_posix()}') "
         f"WHERE ST_Intersects(geometry, ST_MakeEnvelope({minx}, {miny}, {maxx}, {maxy}))")
    roads = con.sql(q).df()
    bridges = set(con.sql(f"SELECT osm_id FROM read_parquet('{(LAKE / 'bridges/bridges.parquet').as_posix()}')").df().osm_id)
    con.close()
    geoms = shapely.from_wkb(roads["wkb"].map(bytes).to_numpy())
    keep = shapely.intersects(geoms, region_poly)
    roads, geoms = roads[keep].reset_index(drop=True), geoms[keep]
    roads["is_bridge"] = roads["osm_id"].isin(bridges)
    roads["rail"] = roads["kind"].eq("railway")
    roads = roads[~roads["rail"]].reset_index(drop=True)
    geoms = geoms[~keep[keep]] if False else shapely.from_wkb(roads["wkb"].map(bytes).to_numpy())
    return roads, geoms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="UA26", help="КАТОТТГ prefix of the oblast (UA26 = Ivano-Frankivsk)")
    ap.add_argument("--buffer-km", type=float, default=15.0, help="roads beyond the border that routes may use")
    ap.add_argument("--a-min", type=float, default=50.0, help="smallest river catchment (km²) that floods, A_MIN")
    a = ap.parse_args()
    t0 = time.time()
    adm3 = gpd.read_parquet(ADM3)
    adm3 = adm3[["pcode", "geometry"] + [c for c in ("name", "name_uk", "adm3_name", "ADM3_UA") if c in adm3.columns]]
    reg = adm3[adm3["pcode"].str.startswith(a.region)]
    region = reg.union_all()
    region_buf = gpd.GeoSeries([region], crs=4326).to_crs(3035).buffer(a.buffer_km * 1000).to_crs(4326).iloc[0]
    log(f"region {a.region}: {len(reg)} hromadas")
    roads, geoms = region_roads(region_buf)
    hand_m, area_km2 = flood_layers(region_buf.bounds)
    # every way sampled every SAMPLE_M: under water at L where HAND ≤ L and the stream's catchment ≥ A_MIN
    g3035 = shapely.transform(geoms, lambda c: np.column_stack(T3035.transform(c[:, 0], c[:, 1])))
    L = shapely.length(g3035)
    n_pts = np.maximum(1, np.ceil(L / SAMPLE_M)).astype(int)
    wi = np.repeat(np.arange(len(geoms)), n_pts)
    frac = (np.concatenate([np.arange(k) for k in n_pts]) + 0.5) / np.repeat(n_pts, n_pts)
    pts = shapely.line_interpolate_point(geoms[wi], frac, normalized=True)
    plon, plat = shapely.get_x(pts), shapely.get_y(pts)
    ph, pa = hand_m(plon, plat), area_km2(plon, plat)
    step = L[wi] / n_pts[wi]
    cut_way = {}
    for lv in LEVELS:
        wet = (ph <= lv) & (pa >= a.a_min)
        inside = np.bincount(wi, weights=wet * step, minlength=len(geoms))
        cut_way[lv] = ((inside >= CUT_MIN_M) | ((inside >= CUT_SHARE * L) & (inside >= CUT_SHORT_M))) & ~roads["is_bridge"].to_numpy()
    log(f"{len(pts):,} road points sampled; A_MIN {a.a_min:g} km²")
    # the flood covers the REGION; ways of the buffer outside it stay passable (routes may leave the oblast)
    in_reg = shapely.intersects(geoms, region)
    for lv in LEVELS:
        cut_way[lv] &= in_reg
    # graph: consecutive vertices of each way; shared OSM nodes = identical coordinates
    xy, way = shapely.get_coordinates(geoms, return_index=True)
    key = np.round(xy * 1e7).astype(np.int64)
    _, node = np.unique(key[:, 0] * 4_000_000_000 + key[:, 1], return_inverse=True)
    n_nodes = int(node.max()) + 1
    same = way[1:] == way[:-1]
    ea, eb, ew = node[:-1][same], node[1:][same], way[:-1][same]
    px, py = T3035.transform(xy[:, 0], xy[:, 1])
    seg_m = np.hypot(np.diff(px), np.diff(py))[same]
    rank = roads["rank"].fillna(6).astype(int).clip(1, 7).to_numpy()
    speed = np.vectorize(SPEED_KMH.get)(rank[ew]).astype(float)
    t_min = seg_m / (speed * 1000 / 60)
    # capacity per way: state-road estimate, else by class
    cap_way = np.vectorize(CAP_DEFAULT.get)(rank).astype(float)
    if CATEGORY_CSV.exists():
        cat = pd.read_csv(CATEGORY_CSV, usecols=["osm_id", "capacity_vph"]).dropna()
        m = roads["osm_id"].astype(str).map(dict(zip(cat.osm_id.astype(str), cat.capacity_vph)))
        cap_way = np.where(m.notna(), m.to_numpy(dtype=float, na_value=np.nan), cap_way)
    log(f"graph: {len(roads):,} ways, {n_nodes:,} nodes, {len(ea):,} edges; "
        + ", ".join(f"{lv} m: {int(cut_way[lv].sum()):,} ways cut" for lv in LEVELS))
    first = np.zeros(n_nodes, dtype=np.int64)
    first[node[::-1]] = np.arange(len(node))[::-1]
    nxy = np.column_stack([px[first], py[first]])

    def graph(keep):
        return sparse.coo_matrix((t_min[keep] + 1e-6, (ea[keep], eb[keep])), shape=(n_nodes, n_nodes)).tocsr()

    dry_keep = np.ones(len(ea), dtype=bool)
    _, lab0 = connected_components(graph(dry_keep), directed=False)
    main_cc = np.bincount(lab0).argmax()
    on_dry = lab0 == main_cc
    # settlements of the region
    s = settlements(adm3[["pcode", "geometry"]])
    s = s[s["pcode"].fillna("").str.startswith(a.region)].reset_index(drop=True)
    sx, sy = T3035.transform(s.geometry.x.to_numpy(), s.geometry.y.to_numpy())
    s["hand_m"] = hand_m(s.geometry.x.to_numpy(), s.geometry.y.to_numpy())
    s["stream_km2"] = area_km2(s.geometry.x.to_numpy(), s.geometry.y.to_numpy())
    tree = cKDTree(nxy[on_dry])
    dry_ids = np.nonzero(on_dry)[0]
    near_d, near_i = tree.query(np.column_stack([sx, sy]), k=8, distance_upper_bound=ACCESS_M)
    access = [dry_ids[i[np.isfinite(d)]] for d, i in zip(near_d, near_i)]
    s["no_road"] = [len(x) == 0 for x in access]
    log(f"settlements: {len(s):,}, no road {int(s.no_road.sum())}")
    # fire stations of the region (+ buffer)
    crit = gpd.read_parquet(CRIT)
    fire = crit[(crit["subtype"] == "fire_station") & crit.within(region_buf)]
    fx, fy = T3035.transform(fire.geometry.x.to_numpy(), fire.geometry.y.to_numpy())
    fd, fi = tree.query(np.column_stack([fx, fy]), k=1, distance_upper_bound=ACCESS_M)
    fire_nodes = np.unique(dry_ids[fi[np.isfinite(fd)]])
    log(f"fire stations: {len(fire)} ({len(fire_nodes)} on the network)")

    def best(dist, acc):
        return min((dist[n] for n in acc), default=np.inf) if len(acc) else np.inf

    rows, routes, cuts = [], [], []
    G0 = graph(dry_keep)
    rescue0 = dijkstra(G0, directed=False, indices=fire_nodes, min_only=True) if len(fire_nodes) else np.full(n_nodes, np.inf)
    pop = s["population"].fillna(0).to_numpy()
    place = s["place"].to_numpy()
    for lv in LEVELS:
        keep = ~cut_way[lv][ew]
        G = graph(keep)
        _, lab = connected_components(G, directed=False)
        # the regional network at this level = the component holding the most dry-network nodes outside the flood
        big = np.bincount(lab[on_dry]).argmax()
        safe = (s["hand_m"].to_numpy() >= lv + SAFE_MARGIN_M) & ((np.isin(place, ["city", "town"])) | (pop >= SAFE_VILLAGE_POP))
        safe_nodes = np.unique(np.concatenate([acc for acc, ok in zip(access, safe) if ok and len(acc)] or [np.array([], int)]))
        safe_nodes = safe_nodes[lab[safe_nodes] == big]
        dist, pred, src = dijkstra(G, directed=False, indices=safe_nodes, min_only=True, return_predecessors=True)
        dist0, _, src0 = dijkstra(G0, directed=False, indices=safe_nodes, min_only=True, return_predecessors=True)
        rescue = dijkstra(G, directed=False, indices=fire_nodes, min_only=True) if len(fire_nodes) else np.full(n_nodes, np.inf)
        # edge lookup for the bottleneck along a path
        e_cap = cap_way[ew]
        pair = pd.Series(np.arange(len(ea)), index=pd.MultiIndex.from_arrays([np.minimum(ea, eb), np.maximum(ea, eb)]))
        pair = pair[~pair.index.duplicated()]
        dest_name = {}
        for i, acc in enumerate(access):
            if not len(acc):
                continue
            for n in acc:
                dest_name.setdefault(int(n), i)
        for i, acc in enumerate(access):
            in_zone = s.at[i, "hand_m"] <= lv and s.at[i, "stream_km2"] >= a.a_min
            isolated = bool(len(acc)) and all(lab[n] != big for n in acc)
            r = {"level_m": lv, "osm_id": s.at[i, "osm_id"], "in_zone": bool(in_zone), "isolated": isolated, "route_min": None,
                 "dry_min": None, "route_km": None, "bottleneck_vph": None, "dest": None, "rescue_min": None, "rescue_dry_min": None}
            if len(acc):
                k = min(acc, key=lambda n: dist[n])
                if np.isfinite(dist[k]) and in_zone:
                    path, n = [], int(k)
                    while n >= 0 and n != src[k]:
                        path.append(n); n = int(pred[n])
                    path.append(int(src[k]))
                    caps = []
                    for u, v in zip(path[:-1], path[1:]):
                        j = pair.get((min(u, v), max(u, v)))
                        if j is not None:
                            caps.append(e_cap[j])
                    d_i = dest_name.get(int(src[k]))
                    r.update(route_min=round(float(dist[k]), 1), dry_min=round(float(best(dist0, acc)), 1),
                             route_km=round(float(sum(np.hypot(*(nxy[u] - nxy[v])) for u, v in zip(path[:-1], path[1:]))) / 1000, 1),
                             bottleneck_vph=float(np.nanmin(caps)) if caps else None,
                             dest=s.at[d_i, "name"] if d_i is not None else None)
                    if len(path) > 1:
                        line = shapely.LineString(nxy[path])
                        routes.append({"level_m": lv, "osm_id": s.at[i, "osm_id"], "name": s.at[i, "name"], "dest": r["dest"],
                                       "route_min": r["route_min"], "dry_min": r["dry_min"], "geometry": line})
                rr, r0 = best(rescue, acc), best(rescue0, acc)
                r.update(rescue_min=round(float(rr), 1) if np.isfinite(rr) else None, rescue_dry_min=round(float(r0), 1) if np.isfinite(r0) else None)
            rows.append(r)
        c = roads[cut_way[lv]][["osm_id", "name", "ref", "kind", "rank", "len_m", "pcode"]].assign(level_m=lv)
        c["geometry"] = geoms[cut_way[lv]]
        cuts.append(c)
        z = [r for r in rows if r["level_m"] == lv]
        log(f"level {lv} m: in the zone {sum(r['in_zone'] for r in z)}, cut off {sum(r['isolated'] for r in z)}, "
            f"with a route {sum(r['route_min'] is not None for r in z)}, rescue lost {sum(r['rescue_min'] is None and r['rescue_dry_min'] is not None for r in z)}")
    out = LAKE / "evacuation" / f"{a.region}_a{a.a_min:g}"
    out.mkdir(parents=True, exist_ok=True)
    res = pd.DataFrame(rows).merge(s[["osm_id", "name", "place", "population", "pcode", "hand_m", "stream_km2", "no_road", "geometry"]], on="osm_id")
    res["hand_m"] = res["hand_m"].replace(np.inf, np.nan)
    gpd.GeoDataFrame(res, geometry="geometry", crs=4326).to_parquet(out / "settlements.parquet", index=False)
    gpd.GeoDataFrame(routes, geometry="geometry", crs=3035).to_crs(4326).to_parquet(out / "routes.parquet", index=False)
    gpd.GeoDataFrame(pd.concat(cuts, ignore_index=True), geometry="geometry", crs=4326).to_parquet(out / "cut_roads.parquet", index=False)
    summ = {"region": a.region, "levels": {}, "assumptions": {"speed_kmh_by_rank": SPEED_KMH, "capacity_default_vph": CAP_DEFAULT,
            "safe": f"town/city or village ≥ {SAFE_VILLAGE_POP} people, point ≥ L + {SAFE_MARGIN_M} m above the stream",
            "cut": f"≥ {CUT_MIN_M} m or ≥ {CUT_SHARE:.0%} of the way within L (≥ {CUT_SHORT_M} m), bridges not cut",
            "flood": f"the whole region at once: HAND ≤ L above a river of ≥ {a.a_min:g} km²; roads outside the region stay passable"}}
    for lv in LEVELS:
        z = res[res.level_m == lv]
        zz = z[z.in_zone]
        det = (zz.route_min - zz.dry_min).dropna()
        summ["levels"][str(lv)] = {"settlements": int(len(z)), "in_zone": int(z.in_zone.sum()), "cut_off": int(z.isolated.sum()),
                                   "in_zone_with_route": int(zz.route_min.notna().sum()), "detour_min_median": round(float(det.median()), 1) if len(det) else None,
                                   "ways_cut": int(cut_way[lv].sum()), "rescue_lost": int((z.rescue_min.isna() & z.rescue_dry_min.notna()).sum())}
    (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1))
    log(f"done in {time.time() - t0:.0f} s → {out}")


if __name__ == "__main__":
    main()
