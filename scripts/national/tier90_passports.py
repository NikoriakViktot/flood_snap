"""
Basin passports for the large (> 20,000 km²) gauges that stay at 90 m: geometry and area from the
national pass, elevation statistics from the 90 m DEM. Danube gauges (basin not in the DEM coverage)
are written with status "outside_dem_coverage".

Run inside the dem_worker image:
  docker run --rm -v ./data:/data -v ./scripts:/scripts projects-dem_worker \
    python3 /scripts/national/tier90_passports.py --dem copernicus_dem
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import rasterio
from rasterio.mask import mask as rio_mask
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform

EPSG = "EPSG:3035"
DANUBE = "5.3.4"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", default="copernicus_dem")
    ap.add_argument("--root", default="/data/national")
    a = ap.parse_args()
    root = Path(a.root) / a.dem
    n90 = root / "90m"
    run = json.loads((n90 / "run.json").read_text())
    rows = {r["post_id"]: r for r in csv.DictReader(open(n90 / "gauges_90m.csv"))}
    feats = {str(f["properties"]["post_id"]): f for f in json.loads((n90 / "basins_90m.geojson").read_text())["features"]}
    to_3035 = Transformer.from_crs("EPSG:4326", EPSG, always_xy=True).transform

    with rasterio.open(n90 / "dem.tif") as dem:
        px_km2 = abs(dem.transform.a * dem.transform.e) / 1e6
        for pid, r in rows.items():
            if r["status"] != "ok" or r["tier"] != "90m":
                continue
            out = root / "gauges" / f"post_id_{pid}"
            out.mkdir(parents=True, exist_ok=True)
            geom_wgs = shape(feats[pid]["geometry"])
            geom = shp_transform(to_3035, geom_wgs)
            data, _ = rio_mask(dem, [mapping(geom)], crop=True, filled=False)
            elev = data[0].compressed()
            F = float(r["official_km2"]) if r["official_km2"] else None
            area = float(r["area_90m_km2"])
            # Danube mainstem/delta: the basin (817k km²) is outside the DEM coverage on purpose
            outside = r["basin_code"] == DANUBE and "Дунай" in (r["river"] or "")
            stats = {
                "area_km2": round(area, 1),
                "perimeter_km": round(geom.length / 1000, 1),
                "compactness_gravelius": round(0.282 * geom.length / 1000 / math.sqrt(area), 3),
                "relief_m": round(float(elev.max() - elev.min()), 1) if elev.size else None,
                "mean_elev_m": round(float(elev.mean()), 1) if elev.size else None,
                "min_elev_m": round(float(elev.min()), 1) if elev.size else None,
                "max_elev_m": round(float(elev.max()), 1) if elev.size else None,
                "centroid_lonlat": [round(geom_wgs.centroid.x, 5), round(geom_wgs.centroid.y, 5)],
                "n_cells": int(round(area / px_km2)),
                "pixel_area_m2": px_km2 * 1e6,
            }
            m = {
                "schema": "basin.manifest.v1",
                "status": "outside_dem_coverage" if outside else "ok",
                "basin_id": "g" + "".join("abcdefghij"[int(d)] for d in pid),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "method": {
                    "version": "national_90m / wbt-d8-v1",
                    "note": "large basin kept at 90 m (EPSG:3035 equal area); no HAND scenarios at this scale",
                    "conditioning": "breach_depressions(fill_pits=True)", "flow_routing": "D8",
                    "outlet_snapping": r["snap_method"],
                },
                "dem": {"source": a.dem, "processing_crs": EPSG, "grid_resolution_m": 90,
                        "vertical_datum": "EGM2008", "vrt": run["vrt"]},
                "software": {"whitebox": run["whitebox"]},
                "gauge": {"post_id": int(pid), "name": r["name"], "river": r["river"], "basin_code": r["basin_code"],
                          "official_area_km2": F, "source": "hydro-ua /v1/hydro/stations"},
                "outlet": {"requested": [float(r["lon"]), float(r["lat"])],
                           "actual_snap_distance_m": float(r["snap_m"]),
                           "area_ratio_to_official": float(r["area_ratio"]) if r["area_ratio"] else None,
                           "upstream_gauges": int(r["n_upstream_outlets"])},
                "stats": stats,
                "quality": {"flag": "outside_dem_coverage" if outside else r["flag"]},
                "vectors": {"basin": "vectors/basin.geojson"},
            }
            (out / "vectors").mkdir(exist_ok=True)
            (out / "vectors" / "basin.geojson").write_text(json.dumps(
                {"type": "Feature", "geometry": mapping(geom_wgs), "properties": {"post_id": int(pid), **stats}}))
            (out / "manifest.json").write_text(json.dumps(m, indent=2, ensure_ascii=False))
            print(f"{pid} {r['name'][:40]:40} {area:>10.0f} km² F={F} {m['status']}", flush=True)


if __name__ == "__main__":
    main()
