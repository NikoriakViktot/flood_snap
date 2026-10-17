"""
Infrastructure and population exposure (stage 0.2 / 0.3 of the ДСНС / hromada plan, geoai-frontend-v2 docs/LABS.md §7a):
the terrain values that buildings already carry (buildings_exposure.py), now for the OSM risk layers and for people.

Per feature (OSM 2026-10-12, deploy/server/risk_layers in data_dnipro_h_q, ODbL):
  critical_sites  (points)  hand_fab_dm, hand_cop_dm, ffpi_x10 at the point
  bridges         (lines)   the same at both approaches (line ends); `*_min` = the lower approach — the deck itself spans
                            the river, so HAND under it says nothing
  roads           (lines)   sampled every SAMPLE_M (EPSG:3035 metres): hand_min_fab_dm / hand_min_cop_dm along the segment,
                            len_m, and len_m_hand_le_{1,2,3,5,10}m (FABDEM) = the segment's length whose ground lies within
                            L metres above the nearest stream
  pcode           hromada (COD-AB v05 ADM3) of the point / the line's midpoint
Population (WorldPop 2020 100 m constrained): every pixel's people counted in its hromada and in the HAND masks
1 / 2 / 3 / 5 / 10 m at the pixel centre (FABDEM; Copernicus at 2 and 5 m).
A HAND mask is a screening: the ground lies within L metres above the nearest stream — not a flood depth, not a probability.

Outputs (data/s3_staging/hydro/lake/exposure/v1/):
  critical_sites/critical_sites.parquet, bridges/bridges.parquet, roads/roads.parquet   EPSG:4326 (roads / bridges
                                   sorted by pcode so a hromada reads one slice)
  infra_hromadas/stats.parquet     per hromada: critical sites (total, by category, in HAND ≤ 2 / 5 m), bridges (total, in
                                   HAND ≤ 1 / 2 / 5 m), road km (total, main roads rank ≤ 3, in HAND ≤ 1 / 2 / 3 / 5 / 10 m),
                                   people (total, in HAND ≤ 1 / 2 / 3 / 5 / 10 m FABDEM, ≤ 2 / 5 m Copernicus)
  manifest.json                    gains the `infrastructure` and `population` sections

  ~/repo/data_dnipro_h_q/.venv-linux/bin/python scripts/national/infra_exposure.py [--only critical,bridges,roads,population]
"""
from __future__ import annotations

import argparse
import gc
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import rasterio
import shapely
from pyproj import Transformer
from rasterio import features
from rasterio.windows import Window

from buildings_exposure import ADM3, LAKE, RASTERS, Sampler

OSM = Path.home() / "gis-sources" / "osm" / "out" / "risk_layers.gpkg"
POP = Path(__file__).resolve().parents[2] / "data/national/_sources/ukr_population_2020_100m_constrained_v2/ukr_pop_2020_100m_constrained_v2.tif"
LEVELS = (1, 2, 3, 5, 10)
SAMPLE_M = 30.0
ROAD_CHUNK = 250_000
T3857 = Transformer.from_crs(4326, 3857, always_xy=True)
T3035 = Transformer.from_crs(4326, 3035, always_xy=True)
T3035_3857 = Transformer.from_crs(3035, 3857, always_xy=True)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def hromada_of(points: gpd.GeoSeries, adm3: gpd.GeoDataFrame) -> np.ndarray:
    j = gpd.sjoin(gpd.GeoDataFrame(geometry=points.values, crs=4326), adm3, how="left", predicate="within")
    j = j[~j.index.duplicated()]
    return j["pcode"].reindex(range(len(points))).to_numpy()


def sample_all(x3857: np.ndarray, y3857: np.ndarray, keys=("hand_fab_dm", "hand_cop_dm", "ffpi_x10")) -> dict[str, np.ndarray]:
    """One raster in memory at a time (each national COG is ~4.5 GB as Byte)."""
    out = {}
    for k in keys:
        p, nd = RASTERS[k]
        s = Sampler(p, nd)
        out[k] = s(x3857, y3857)
        del s
        gc.collect()
    return out


def do_critical(adm3) -> pd.DataFrame:
    g = pyogrio.read_dataframe(OSM, layer="critical_sites")
    x, y = T3857.transform(g.geometry.x.to_numpy(), g.geometry.y.to_numpy())
    for k, v in sample_all(np.asarray(x), np.asarray(y)).items():
        g[k] = v
    g["pcode"] = hromada_of(g.geometry, adm3)
    out = LAKE / "critical_sites"
    out.mkdir(parents=True, exist_ok=True)
    g.to_parquet(out / "critical_sites.parquet", index=False)
    log("critical sites", len(g))
    return pd.DataFrame(g.drop(columns="geometry"))


def do_bridges(adm3) -> pd.DataFrame:
    g = pyogrio.read_dataframe(OSM, layer="risk_bridges")
    a = shapely.get_point(g.geometry.values, 0)
    b = shapely.get_point(g.geometry.values, -1)
    xa, ya = T3857.transform(shapely.get_x(a), shapely.get_y(a))
    xb, yb = T3857.transform(shapely.get_x(b), shapely.get_y(b))
    sa = sample_all(np.asarray(xa), np.asarray(ya))
    sb = sample_all(np.asarray(xb), np.asarray(yb))
    for k in sa:
        nd = RASTERS[k][1]
        va, vb = sa[k].astype(np.int16), sb[k].astype(np.int16)
        if nd == 255:   # HAND: 255 = higher / no data → the lower approach that has a value
            va[va == 255] = 999
            vb[vb == 255] = 999
            m = np.minimum(va, vb)
            m[m == 999] = 255
        else:           # FFPI: 0 = no data → the higher potential
            m = np.maximum(va, vb)
        g[f"{k}_min" if nd == 255 else f"{k}_max"] = m.astype(np.uint8)
    g["pcode"] = hromada_of(gpd.GeoSeries(shapely.line_interpolate_point(g.geometry.values, 0.5, normalized=True), crs=4326), adm3)
    out = LAKE / "bridges"
    out.mkdir(parents=True, exist_ok=True)
    g.sort_values("pcode").to_parquet(out / "bridges.parquet", index=False, row_group_size=5000)
    log("bridges", len(g))
    return pd.DataFrame(g.drop(columns="geometry"))


def do_roads(adm3) -> pd.DataFrame:
    n_total = pyogrio.read_info(OSM, layer="risk_roads")["features"]
    out = LAKE / "roads"
    out.mkdir(parents=True, exist_ok=True)
    samplers = {k: Sampler(*RASTERS[k]) for k in ("hand_fab_dm", "hand_cop_dm")}   # ~9 GB: both HAND rasters stay loaded
    log("road rasters loaded")
    stats = []
    for part, off in enumerate(range(0, n_total, ROAD_CHUNK)):
        g = pyogrio.read_dataframe(OSM, layer="risk_roads", skip_features=off, max_features=ROAD_CHUNK)
        geoms = g.geometry.values
        g3035 = shapely.transform(geoms, lambda xy: np.column_stack(T3035.transform(xy[:, 0], xy[:, 1])))
        L = shapely.length(g3035)
        n = np.maximum(2, np.ceil(L / SAMPLE_M).astype(np.int64) + 1)
        idx = np.repeat(np.arange(len(g)), n)
        starts = np.cumsum(n) - n
        frac = (np.arange(idx.size) - starts[idx]) / (n[idx] - 1)
        pts = shapely.line_interpolate_point(g3035[idx], frac, normalized=True)
        x, y = T3035_3857.transform(shapely.get_x(pts), shapely.get_y(pts))
        x, y = np.asarray(x), np.asarray(y)
        for k, s in samplers.items():
            v = s(x, y).astype(np.int16)
            v[v == 255] = 999
            mn = np.minimum.reduceat(v, starts)
            mn[mn == 999] = 255
            g[k.replace("hand_", "hand_min_")] = mn.astype(np.uint8)
            if k == "hand_fab_dm":
                # each sample stands for an equal share of the segment's length
                for lv in LEVELS:
                    share = np.add.reduceat((v <= 10 * lv).astype(np.float64), starts) / n
                    g[f"len_m_hand_le_{lv}m"] = (share * L).astype(np.float32)
        g["len_m"] = L.astype(np.float32)
        g["pcode"] = hromada_of(gpd.GeoSeries(shapely.line_interpolate_point(geoms, 0.5, normalized=True), crs=4326), adm3)
        g.to_parquet(out / f"part-{part:03d}.parquet", index=False)
        stats.append(pd.DataFrame(g.drop(columns="geometry")))
        log(f"roads part {part}: {off + len(g)}/{n_total}, {idx.size / 1e6:.1f} M samples")
        del g, g3035, pts, x, y
        gc.collect()
    del samplers
    gc.collect()
    # one file sorted by hromada (row groups of 20 000) so a hromada's roads are one slice for the Hydro API
    import duckdb
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * FROM read_parquet('{out.as_posix()}/part-*.parquet') ORDER BY pcode, rank) TO "
                f"'{(out / 'roads.parquet').as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 20000)")
    con.close()
    for p in out.glob("part-*.parquet"):
        p.unlink()
    return pd.concat(stats, ignore_index=True)


def do_population(adm3) -> pd.DataFrame:
    """People per hromada and per HAND mask: WorldPop pixel centres sampled on both HAND rasters (row blocks)."""
    with rasterio.open(POP) as r:
        pop_t, h, w, nd = r.transform, r.height, r.width, r.nodata
        codes = adm3["pcode"].tolist()
        zone = features.rasterize(((geom, i + 1) for i, geom in enumerate(adm3.geometry)), out_shape=(h, w), transform=pop_t, dtype="int16")
        log("hromadas rasterised on the WorldPop grid")
        acc = {key: np.zeros(len(codes) + 1) for key in ["total"] + [f"fab_{lv}" for lv in LEVELS] + ["cop_2", "cop_5"]}
        samplers = {k: Sampler(*RASTERS[k]) for k in ("hand_fab_dm", "hand_cop_dm")}
        step = 512
        for r0 in range(0, h, step):
            win = Window(0, r0, w, min(step, h - r0))
            p = r.read(1, window=win).astype(np.float64)
            p[(p == nd) | ~np.isfinite(p) | (p < 0)] = 0.0
            rows, cols = np.nonzero(p > 0)
            if not rows.size:
                continue
            vals = p[rows, cols]
            z = zone[r0 + rows, cols]
            lon, lat = rasterio.transform.xy(pop_t, r0 + rows, cols, offset="center")
            x, y = T3857.transform(np.asarray(lon), np.asarray(lat))
            x, y = np.asarray(x), np.asarray(y)
            acc["total"] += np.bincount(z, weights=vals, minlength=len(codes) + 1)
            hf = samplers["hand_fab_dm"](x, y)
            for lv in LEVELS:
                m = hf <= 10 * lv
                acc[f"fab_{lv}"] += np.bincount(z[m], weights=vals[m], minlength=len(codes) + 1)
            hc = samplers["hand_cop_dm"](x, y)
            for lv in (2, 5):
                m = hc <= 10 * lv
                acc[f"cop_{lv}"] += np.bincount(z[m], weights=vals[m], minlength=len(codes) + 1)
        del samplers
        gc.collect()
    df = pd.DataFrame({"pcode": codes, "pop_total": acc["total"][1:],
                       **{f"pop_hand_le_{lv}m": acc[f"fab_{lv}"][1:] for lv in LEVELS},
                       **{f"pop_hand_cop_le_{lv}m": acc[f"cop_{lv}"][1:] for lv in (2, 5)}})
    outside = acc["total"][0]
    log(f"population: {df.pop_total.sum():,.0f} in hromadas, {outside:,.0f} outside any hromada polygon")
    return df.round(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="critical,bridges,roads,population")
    a = ap.parse_args()
    todo = set(a.only.split(","))
    t0 = time.time()
    adm3 = gpd.read_parquet(ADM3)[["pcode", "geometry"]].reset_index(drop=True)
    parts = {}
    if "critical" in todo:
        c = do_critical(adm3)
        cat = c.pivot_table(index="pcode", columns="category", values="osm_id", aggfunc="count", fill_value=0).add_prefix("n_critical_")
        parts["critical"] = pd.concat([c.groupby("pcode").size().rename("n_critical"),
                                       c[c.hand_fab_dm <= 20].groupby("pcode").size().rename("n_critical_hand_le_2m"),
                                       c[c.hand_fab_dm <= 50].groupby("pcode").size().rename("n_critical_hand_le_5m"), cat], axis=1)
    if "bridges" in todo:
        b = do_bridges(adm3)
        parts["bridges"] = pd.concat([b.groupby("pcode").size().rename("n_bridges")] +
                                     [b[b.hand_fab_dm_min <= 10 * lv].groupby("pcode").size().rename(f"n_bridges_hand_le_{lv}m") for lv in (1, 2, 5)], axis=1)
    if "roads" in todo:
        r = do_roads(adm3)
        main_roads = r[r["rank"] <= 3]
        parts["roads"] = pd.concat([r.groupby("pcode")["len_m"].sum().div(1000).rename("road_km"),
                                    main_roads.groupby("pcode")["len_m"].sum().div(1000).rename("road_km_main")] +
                                   [r.groupby("pcode")[f"len_m_hand_le_{lv}m"].sum().div(1000).rename(f"road_km_hand_le_{lv}m") for lv in LEVELS] +
                                   [main_roads.groupby("pcode")[f"len_m_hand_le_{lv}m"].sum().div(1000).rename(f"road_km_main_hand_le_{lv}m") for lv in (2, 5)], axis=1)
    if "population" in todo:
        parts["population"] = do_population(adm3).set_index("pcode")
    stats_path = LAKE / "infra_hromadas" / "stats.parquet"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    old = pd.read_parquet(stats_path).set_index("pcode") if stats_path.exists() else pd.DataFrame(index=adm3.pcode)
    for k, p in parts.items():
        old = old.drop(columns=[c for c in p.columns if c in old.columns]).join(p, how="left")
    st = old.reindex(adm3.pcode)
    for c in st.columns:
        if c.startswith("n_"):
            st[c] = st[c].fillna(0).astype("int32")
        elif c.startswith(("road_km", "pop_")):
            st[c] = st[c].fillna(0.0).astype("float64").round(2)
    st.reset_index().to_parquet(stats_path, index=False)
    man_p = LAKE / "manifest.json"
    man = json.loads(man_p.read_text()) if man_p.exists() else {"product": "exposure", "version": "v1"}
    man.setdefault("sources", {})["osm_risk_layers"] = "OpenStreetMap via Geofabrik ukraine-latest 2026-10-12 (ODbL): risk_roads, risk_bridges, critical_sites"
    man["sources"]["population"] = "WorldPop 2020 100 m constrained v2 (CC BY 4.0)"
    man["infrastructure"] = {"created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             "method": "HAND / FFPI at critical sites (point), bridges (both approaches, the lower), roads (every 30 m, "
                                       "min and length per HAND level); screening, not a flood depth",
                             "files": ["critical_sites/critical_sites.parquet", "bridges/bridges.parquet", "roads/roads.parquet",
                                       "infra_hromadas/stats.parquet"]}
    if "population" in todo:
        man["population"] = {"method": "WorldPop pixel centres sampled on the national HAND rasters; people per hromada and per HAND "
                                       "mask 1/2/3/5/10 m (FABDEM), 2/5 m (Copernicus); screening",
                             "total": round(float(st.pop_total.sum()))}
    man_p.write_text(json.dumps(man, ensure_ascii=False, indent=1))
    log(f"done in {time.time() - t0:.0f} s → {stats_path} ({len(st)} hromadas)")


if __name__ == "__main__":
    main()
