"""
All gauge basins of the national precompute → GeoParquet for fast server-side access.

One row per gauge and DEM source: the best available delineation (30 m passport when present and
usable, otherwise the 90 m national pass). Columns carry the passport statistics, quality flags and
the S3 key of the full manifest, so the server needs nothing else for listing/filtering basins.

Writes (GeoParquet 1.1, EPSG:4326, bbox covering column, rows sorted along a Hilbert curve):
  <staging>/hydro/lake/basin_delineation/v1/gauge_basins/dem_source=<dem>/basins.parquet
  <staging>/hydro/lake/basin_delineation/v1/gauge_basins_simplified/dem_source=<dem>/basins.parquet
  <staging>/…/dem_source=<dem>/_manifest.json   (leading "_": ignored by pyarrow.dataset)

Needs geopandas ≥ 1.0 + pyarrow (e.g. ~/repo/SWOT-DNIPRO/.venv/bin/python):
  python scripts/national/build_basins_geoparquet.py --dem copernicus_dem
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import shape

LAKE = "hydro/lake/basin_delineation/v1"
SIMPLIFY_DEG = 0.001  # ≈ 70–110 m: map display


def passport_row(pid: str, m: dict, geom, dem: str) -> dict:
    st, o, q = m.get("stats", {}), m.get("outlet", {}), m.get("quality", {})
    res = m.get("dem", {}).get("grid_resolution_m")
    failed = [c["name"] for c in q.get("checks", []) if not c.get("ok")]
    status = m.get("status", "ok")
    g = m.get("gauge", {})
    # gauge.official_area_km2 is the yearbook F; outlet.official_area_km2 may hold the 90 m area that
    # was only used as a snapping target for gauges without F — never report that as "official"
    official = g.get("official_area_km2") if g else o.get("official_area_km2")
    ratio = round(st["area_km2"] / official, 4) if official else None
    flag = q.get("flag") or ("outside_dem_coverage" if status == "outside_dem_coverage" else
                             "area_mismatch" if ratio and not 0.9 <= ratio <= 1.1 else
                             "no_official_area" if not official else "ok")
    return {
        "post_id": int(pid), "name": g.get("name"), "river": g.get("river"), "basin_code": g.get("basin_code"),
        "dem_source": dem, "resolution_m": int(res) if res else None,
        "method": m.get("method", {}).get("version"), "status": status, "flag": flag,
        "official_area_km2": official, "area_km2": st.get("area_km2"), "area_ratio": ratio,
        "snap_distance_m": o.get("actual_snap_distance_m"), "snap_warning": o.get("snap_warning"),
        "stream_order": st.get("stream_order"), "longest_flowpath_km": st.get("longest_flowpath_km"),
        "flowpath_slope_m_per_m": st.get("longest_flowpath_slope_m_per_m"),
        "relief_m": st.get("relief_m"), "mean_elev_m": st.get("mean_elev_m"),
        "mean_slope_deg": st.get("mean_slope_deg"), "perimeter_km": st.get("perimeter_km"),
        "compactness_gravelius": st.get("compactness_gravelius"),
        "drainage_density_km_per_km2": st.get("drainage_density_km_per_km2"),
        "touches_dem_edge": q.get("touches_dem_edge"), "failed_checks": ",".join(failed) or None,
        "outlet_lon": (o.get("snapped") or o.get("requested") or [None, None])[0],
        "outlet_lat": (o.get("snapped") or o.get("requested") or [None, None])[1],
        "manifest_key": f"{LAKE}/gauges/dem_source={dem}/post_id={pid}/manifest.json",
        "created_at": m.get("created_at"),
        "geometry": geom,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", default="copernicus_dem")
    ap.add_argument("--root", default="data/national")
    ap.add_argument("--staging", default="data/s3_staging")
    a = ap.parse_args()

    root = Path(a.root) / a.dem
    rows, sources = [], {"30m": 0, "90m_passport": 0, "90m_fallback": 0}
    seen = set()
    for d in sorted((root / "gauges").glob("post_id_*")):
        mp, gp = d / "manifest.json", d / "vectors" / "basin.geojson"
        if not (mp.exists() and gp.exists()):
            continue
        pid = d.name.split("_")[-1]
        m = json.loads(mp.read_text())
        geom = shape(json.loads(gp.read_text())["geometry"])
        rows.append(passport_row(pid, m, geom, a.dem))
        seen.add(int(pid))
        sources["30m" if m.get("dem", {}).get("grid_resolution_m") == 30 else "90m_passport"] += 1

    # gauges whose 30 m passport is missing (still running, or rejected): fall back to the 90 m pass
    n90 = root / "90m"
    feats = {f["properties"]["post_id"]: f for f in json.loads((n90 / "basins_90m.geojson").read_text())["features"]}
    for r in csv.DictReader(open(n90 / "gauges_90m.csv")):
        pid = int(r["post_id"])
        if pid in seen or r["status"] != "ok" or pid not in feats:
            continue
        F = float(r["official_km2"]) if r["official_km2"] else None
        rows.append({
            "post_id": pid, "name": r["name"], "river": r["river"], "basin_code": r["basin_code"],
            "dem_source": a.dem, "resolution_m": 90, "method": "national_90m / wbt-d8-v1", "status": "ok",
            "flag": r["flag"], "official_area_km2": F, "area_km2": float(r["area_90m_km2"]),
            "area_ratio": float(r["area_ratio"]) if r["area_ratio"] else None,
            "snap_distance_m": float(r["snap_m"]), "manifest_key": None,
            "outlet_lon": float(r["lon"]), "outlet_lat": float(r["lat"]),
            "geometry": shape(feats[pid]["geometry"]),
        })
        sources["90m_fallback"] += 1

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    gdf["geometry"] = gdf.geometry.make_valid()
    # Hilbert order: neighbouring basins end up in the same row groups → cheap bbox filtering
    gdf = gdf.iloc[gdf.geometry.hilbert_distance().argsort()].reset_index(drop=True)

    # dem_source is the hive partition (it lives in the path): storing it in the file as well makes
    # pyarrow fail with "Unable to merge: Field dem_source has incompatible types"
    gdf = gdf.drop(columns=["dem_source"])
    # two hive datasets (full / simplified) so each partition dir holds only its own parquet;
    # the manifest starts with "_" so pyarrow.dataset ignores it
    out = Path(a.staging) / LAKE / "gauge_basins" / f"dem_source={a.dem}"
    out_s = Path(a.staging) / LAKE / "gauge_basins_simplified" / f"dem_source={a.dem}"
    for d in (out, out_s):
        d.mkdir(parents=True, exist_ok=True)
        for stale in ("basins_simplified.parquet", "manifest.json"):
            (d / stale).unlink(missing_ok=True)
    kw = dict(compression="zstd", write_covering_bbox=True, schema_version="1.1.0", row_group_size=64)
    gdf.to_parquet(out / "basins.parquet", **kw)
    simp = gdf.copy()
    simp["geometry"] = simp.geometry.simplify(SIMPLIFY_DEG, preserve_topology=True)
    simp.to_parquet(out_s / "basins.parquet", **kw)

    ok = gdf[gdf["area_ratio"].notna()]
    manifest = {
        "schema": "gauge_basins.v1", "dem_source": a.dem, "crs": "EPSG:4326",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rows": len(gdf), "sources": sources,
        "resolution_counts": {str(k): int(v) for k, v in gdf["resolution_m"].value_counts().items()},
        "within_10pct_of_official": int(((ok["area_ratio"] >= 0.9) & (ok["area_ratio"] <= 1.1)).sum()),
        "with_official_area": len(ok),
        "files": {"basins": f"{LAKE}/gauge_basins/dem_source={a.dem}/basins.parquet",
                  "basins_simplified": f"{LAKE}/gauge_basins_simplified/dem_source={a.dem}/basins.parquet"},
        "format": "GeoParquet 1.1 (zstd, bbox covering column, Hilbert-sorted, 64-row groups)",
        "simplify_tolerance_deg": SIMPLIFY_DEG,
        "gauge_source": "hydro-ua /v1/hydro/stations (snapshot in data/gauges/hydro_ua_river_gauges.json)",
    }
    for d in (out, out_s):
        (d / "_manifest.json").write_text(json.dumps(manifest, indent=2))
    sizes = {"basins": round((out / "basins.parquet").stat().st_size / 2**20, 2),
             "simplified": round((out_s / "basins.parquet").stat().st_size / 2**20, 2)}
    print(json.dumps({**{k: manifest[k] for k in ("rows", "sources", "resolution_counts",
                                                  "within_10pct_of_official", "with_official_area")},
                      "MiB": sizes, "dir": str(out)}, indent=1))


if __name__ == "__main__":
    main()
