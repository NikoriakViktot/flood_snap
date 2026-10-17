"""
Settlements cut off by a flood (ДСНС R3, geoai-frontend-v2 docs/LABS.md §7a): which villages and towns lose every road
link to the national road network when the roads inside a HAND level are impassable.

Graph: every OSM road way of the risk layer (risk_roads, 2.16 M ways, every class incl. tracks) as edges between its
consecutive vertices; ways meet where they share an OSM node, i.e. an identical coordinate. A way is impassable at
level L when a real stretch of it lies inside the level — at least CUT_MIN_M of its length with ground within L m
above the nearest stream, or at least CUT_SHARE of it (and CUT_SHORT_M) — from infra_exposure.py's len_m_hand_le_{L}m.
The lowest point alone is NOT used: every road that crosses a creek on a culvert or an embankment touches HAND ≈ 0 for
a pixel or two, and that test cut 98 % of the network at 1 m. Bridges are never cut themselves (the deck spans the
river; the approaches are the adjacent ways, tested like any road). The national network at L = the largest connected
component of the passable ways.

The flood is LOCAL, per hromada: a uniform HAND level over all of Ukraine at once is no scenario (it fragments the whole
network — 82 % of settlements "cut off" at 2 m in the first test). For hromada H at level L only H's ways are tested
(a way belongs to the hromada of its midpoint); every way outside H stays passable. A settlement of H is cut off when
none of its access nodes can reach, through H's passable ways, a node shared with a way outside H that is on the dry
national network ("the flood in your hromada at level L").

Settlements: OSM place = city / town / village / hamlet (points of ukraine-latest.osm.pbf). The access nodes of a
settlement = the road vertices within ACCESS_M of its point on the dry network (every way passable). A settlement is
cut off at L when none of its access nodes is in the national network at L; `isolated_level_m` = the lowest such L of
1 / 2 / 3 / 5 m (null = never at 5 m). A settlement with no road vertex within ACCESS_M is `no_road` (OSM gap or a
settlement without roads), never counted as cut off.
This is a screening on OSM roads and the national HAND mask: no road heights, embankments, culverts or flood timing.

Outputs (data/s3_staging/hydro/lake/exposure/v1/):
  settlements/settlements.parquet   osm_id, name, name_en, place, population (OSM tag), pcode, geometry (EPSG:4326),
                                    access_nodes, no_road, isolated_le_{1,2,3,5}m, isolated_level_m — sorted by pcode
  infra_hromadas/stats.parquet      gains n_settlements, n_settlements_isolated_le_{1,2,3,5}m and
                                    pop_osm_isolated_le_{2,5}m (sum of the OSM population tags of the cut-off settlements)

  ~/repo/data_dnipro_h_q/.venv-linux/bin/python scripts/national/isolation.py
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import duckdb

WAY_PCODE: np.ndarray
import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
from pyproj import Transformer
from scipy import sparse
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from buildings_exposure import ADM3, LAKE

PBF = Path.home() / "gis-sources" / "osm" / "ukraine-latest.osm.pbf"
LEVELS = (1, 2, 3, 5)
CUT_MIN_M = 150.0
CUT_SHARE = 0.8
CUT_SHORT_M = 60.0
ACCESS_M = 400.0
PLACES = ("city", "town", "village", "hamlet")
T3035 = Transformer.from_crs(4326, 3035, always_xy=True)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def road_graph():
    con = duckdb.connect()
    lens = ", ".join(f"len_m_hand_le_{lv}m" for lv in LEVELS)
    roads = con.sql(f"SELECT osm_id, rank, len_m, {lens}, pcode, geometry FROM read_parquet('{(LAKE / 'roads/roads.parquet').as_posix()}')").df()
    global WAY_PCODE
    WAY_PCODE = roads["pcode"].fillna("").to_numpy()
    bridges = con.sql(f"SELECT osm_id FROM read_parquet('{(LAKE / 'bridges/bridges.parquet').as_posix()}')").df()
    con.close()
    geoms = shapely.from_wkb(roads["geometry"].map(bytes).to_numpy())
    is_bridge = roads["osm_id"].isin(set(bridges.osm_id)).to_numpy()
    L = roads["len_m"].to_numpy()
    cut_way = {}
    for lv in LEVELS:
        inside = roads[f"len_m_hand_le_{lv}m"].to_numpy()
        cut_way[lv] = ((inside >= CUT_MIN_M) | ((inside >= CUT_SHARE * L) & (inside >= CUT_SHORT_M))) & ~is_bridge
    xy, way = shapely.get_coordinates(geoms, return_index=True)
    # vertices shared by ways (an OSM node) have identical coordinates: 1e-7° keys join them
    key = np.round(xy * 1e7).astype(np.int64)
    _, node = np.unique(key[:, 0] * 4_000_000_000 + key[:, 1], return_inverse=True)
    n_nodes = int(node.max()) + 1
    same = way[1:] == way[:-1]
    a, b, ew = node[:-1][same], node[1:][same], way[:-1][same]
    log(f"graph: {len(roads):,} ways ({int(is_bridge.sum()):,} bridges), {n_nodes:,} nodes, {len(a):,} edges; "
        + ", ".join(f"{lv} m: {int(cut_way[lv].sum()):,} ways cut" for lv in LEVELS))
    # one coordinate per node, in EPSG:3035 metres, for the access search
    first = np.zeros(n_nodes, dtype=np.int64)
    first[node[::-1]] = np.arange(len(node))[::-1]
    nx, ny = T3035.transform(xy[first, 0], xy[first, 1])
    return a, b, {lv: c[ew] for lv, c in cut_way.items()}, n_nodes, np.column_stack([nx, ny]), roads.loc[ew, "osm_id"].index.to_numpy(), ew


def national(a, b, keep, n_nodes) -> np.ndarray:
    """Boolean per node: in the largest connected component of the kept edges."""
    g = sparse.coo_matrix((np.ones(int(keep.sum()), dtype=np.int8), (a[keep], b[keep])), shape=(n_nodes, n_nodes))
    _, lab = connected_components(g, directed=False)
    big = np.bincount(lab).argmax()
    return lab == big


def settlements(adm3) -> gpd.GeoDataFrame:
    q = "SELECT osm_id, name, place, other_tags FROM points WHERE place IN ('" + "','".join(PLACES) + "')"
    s = pyogrio.read_dataframe(PBF, sql=q)
    tags = s["other_tags"].fillna("")
    pop = tags.str.extract(r'"population"=>"([0-9 ]+)"', expand=False).str.replace(" ", "")
    s["population"] = pd.to_numeric(pop, errors="coerce")
    s["name_en"] = tags.str.extract(r'"name:en"=>"([^"]+)"', expand=False)
    s = s.drop(columns="other_tags")
    j = gpd.sjoin(s, adm3, how="left", predicate="within")
    j = j[~j.index.duplicated()]
    s["pcode"] = j["pcode"]
    log(f"settlements: {len(s):,} ({s.place.value_counts().to_dict()}), {s.pcode.isna().sum()} outside ADM3")
    return s


def main():
    t0 = time.time()
    adm3 = gpd.read_parquet(ADM3)[["pcode", "geometry"]]
    a, b, cut, n_nodes, nxy, _, ew = road_graph()
    dry = national(a, b, np.ones(len(a), dtype=bool), n_nodes)
    log(f"dry national network: {int(dry.sum()):,} of {n_nodes:,} nodes")
    s = settlements(adm3)
    sx, sy = T3035.transform(s.geometry.x.to_numpy(), s.geometry.y.to_numpy())
    tree = cKDTree(nxy[dry])
    dry_ids = np.nonzero(dry)[0]
    near = tree.query_ball_point(np.column_stack([sx, sy]), r=ACCESS_M)
    access = [dry_ids[np.asarray(n, dtype=np.int64)] for n in near]
    s["access_nodes"] = [len(x) for x in access]
    s["no_road"] = s["access_nodes"] == 0
    # edges per hromada (by the way's pcode); nodes shared with ways of another hromada on the dry network = "outside"
    epc = WAY_PCODE[ew]
    order = np.argsort(epc, kind="stable")
    bounds = np.flatnonzero(np.r_[True, epc[order][1:] != epc[order][:-1], True])
    groups = {epc[order[bounds[k]]]: order[bounds[k]:bounds[k + 1]] for k in range(len(bounds) - 1)}
    # a node is "outside H" when it has an incident edge of another hromada; count distinct pcodes per node
    node_pc = np.concatenate([a, b]), np.concatenate([epc, epc])
    pairs = pd.DataFrame({"n": node_pc[0], "p": node_pc[1]}).drop_duplicates()
    multi = pairs.groupby("n").size()
    border = np.zeros(n_nodes, dtype=bool)
    border[multi.index[multi.to_numpy() > 1].to_numpy()] = True
    border &= dry
    log(f"{len(groups):,} hromadas with roads; {int(border.sum()):,} border nodes")
    level_of = np.full(len(s), np.nan)
    iso = {lv: np.zeros(len(s), dtype=bool) for lv in LEVELS}
    spc = s["pcode"].fillna("").to_numpy()
    by_h = pd.Series(np.arange(len(s))).groupby(spc).apply(list).to_dict()
    for h, idx in by_h.items():
        e = groups.get(h)
        if e is None or not h:
            continue
        ea, eb = a[e], b[e]
        loc, inv = np.unique(np.concatenate([ea, eb]), return_inverse=True)
        la, lb = inv[: len(e)], inv[len(e):]
        n_loc = len(loc)
        outside = border[loc]
        for lv in LEVELS:
            keep = ~cut[lv][e]
            g = sparse.coo_matrix((np.ones(int(keep.sum()), dtype=np.int8), (la[keep], lb[keep])), shape=(n_loc, n_loc))
            _, lab = connected_components(g, directed=False)
            good = np.zeros(lab.max() + 1, dtype=bool)
            good[lab[outside]] = True
            pos = {int(n): k for k, n in enumerate(loc)}
            for i_s in idx:
                acc = access[i_s]
                if not len(acc):
                    continue
                ok = False
                for n in acc:
                    k = pos.get(int(n))
                    if k is None or good[lab[k]]:      # an access node outside H's ways is outside: reachable
                        ok = True
                        break
                iso[lv][i_s] = not ok
    for lv in LEVELS:
        s[f"isolated_le_{lv}m"] = iso[lv]
        level_of[np.isnan(level_of) & iso[lv]] = lv
        log(f"level {lv} m (local flood per hromada): settlements cut off {int(iso[lv].sum()):,}")
    s["isolated_level_m"] = level_of
    out = LAKE / "settlements"
    out.mkdir(parents=True, exist_ok=True)
    s.sort_values("pcode").to_parquet(out / "settlements.parquet", index=False, row_group_size=5000)
    # per hromada
    g = s.groupby("pcode")
    st = pd.DataFrame({"n_settlements": g.size()})
    for lv in LEVELS:
        st[f"n_settlements_isolated_le_{lv}m"] = g[f"isolated_le_{lv}m"].sum()
    for lv in (2, 5):
        st[f"pop_osm_isolated_le_{lv}m"] = s[s[f"isolated_le_{lv}m"]].groupby("pcode")["population"].sum()
    stats_path = LAKE / "infra_hromadas" / "stats.parquet"
    old = pd.read_parquet(stats_path).set_index("pcode")
    old = old.drop(columns=[c for c in st.columns if c in old.columns]).join(st, how="left")
    for c in st.columns:
        old[c] = old[c].fillna(0).astype("int32" if c.startswith("n_") else "float64")
    old.reset_index().to_parquet(stats_path, index=False)
    man_p = LAKE / "manifest.json"
    man = json.loads(man_p.read_text())
    man["settlements"] = {"method": f"OSM road graph (shared nodes); a way is impassable when >= {CUT_MIN_M:.0f} m (or >= {CUT_SHARE:.0%} "
                                    f"and >= {CUT_SHORT_M:.0f} m) of it has ground within the HAND level; bridges never cut; cut off = "
                                    f"no access node within {ACCESS_M:.0f} m in the largest connected component; screening",
                          "places": list(PLACES),
                          "counts": {"settlements": int(len(s)), **{f"isolated_le_{lv}m": int(s[f'isolated_le_{lv}m'].sum()) for lv in LEVELS}},
                          "files": ["settlements/settlements.parquet"]}
    man_p.write_text(json.dumps(man, ensure_ascii=False, indent=1))
    log(f"done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
