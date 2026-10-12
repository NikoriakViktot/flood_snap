"""
Buildings exposure: every OSM building of Ukraine (HOT export 2025-01-11, 6.8 M polygons, ODbL) with the terrain
values at its centroid, and the counts per hromada.

Per building (centroid, EPSG:3035 area):
  hand_fab_dm, hand_cop_dm   HAND in decimetres from the national display COGs (255 = higher than 25.4 m / no data):
                             a building is inside the HAND mask of level L when hand_dm <= 10·L
  ffpi_x10                   Flash Flood Potential Index v2 × 10 (0 = no data), NOT validated
  pcode                      hromada (COD-AB v05 ADM3)
  footprint_m2, levels, building (OSM type), name, osm_id
Per hromada: n_buildings, footprint_km2, n_residential, n_hand_le_{1,2,3,5,10}m (FABDEM), the same for Copernicus at
2 and 5 m, n_ffpi_high (>= 7), n_ffpi_very_high (>= 8.5).

The HAND mask of a building is a screening: it says the ground at the centroid lies within L metres above the
nearest stream, not that the building floods at a given discharge.

Outputs:
  data/s3_staging/hydro/lake/exposure/v1/buildings/buildings.parquet            GeoParquet, polygons + attributes
  data/s3_staging/hydro/lake/exposure/v1/buildings_hromadas/stats.parquet
  data/s3_staging/hydro/lake/exposure/v1/manifest.json
  data/national/buildings_postgis.gpkg   layer buildings (polygons + attributes) for PostGIS / Martin

  ~/repo/data_dnipro_h_q/.venv-linux/bin/python scripts/national/buildings_exposure.py
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import rasterio
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
SRC = Path.home() / "gis-work" / "osm_buildings" / "hotosm_ukr_buildings_polygons_gpkg.gpkg"
LAKE = DATA / "s3_staging" / "hydro" / "lake" / "exposure" / "v1"
RASTERS = {"hand_fab_dm": (DATA / "dem_display/hand_ua/fab_dem_cog.tif", 255),
           "hand_cop_dm": (DATA / "dem_display/hand_ua/copernicus_dem_cog.tif", 255),
           "ffpi_x10": (DATA / "dem_display/ffpi_ua/fab_dem_cog.tif", 0)}
ADM3 = DATA / "s3_staging/hydro/lake/admin/v1/adm3.parquet"
CHUNK = 500_000
RESIDENTIAL = {"house", "residential", "apartments", "detached", "semidetached_house", "terrace", "bungalow", "dormitory", "farm", "cabin", "hut"}


class Sampler:
    """Whole raster in memory (Byte), indexed by the centroids' pixel rows/cols (EPSG:3857)."""

    def __init__(self, path: Path, nodata: int):
        with rasterio.open(path) as r:
            self.arr = r.read(1)
            self.inv = ~r.transform
            self.h, self.w = r.height, r.width
        self.nodata = nodata

    def __call__(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        c, r = self.inv * (x, y)
        r = np.floor(r).astype(np.int64)
        c = np.floor(c).astype(np.int64)
        ok = (r >= 0) & (r < self.h) & (c >= 0) & (c < self.w)
        out = np.full(x.shape, self.nodata, dtype=np.uint8)
        out[ok] = self.arr[r[ok], c[ok]]
        return out


def main():
    t0 = time.time()
    n_total = pyogrio.read_info(SRC)["features"]
    print("buildings:", n_total, flush=True)
    samplers = {k: Sampler(p, nd) for k, (p, nd) in RASTERS.items()}
    print("rasters loaded", round(time.time() - t0), "s", flush=True)
    adm3 = gpd.read_parquet(ADM3)[["pcode", "geometry"]]
    to3857 = Transformer.from_crs(4326, 3857, always_xy=True)
    LAKE.joinpath("buildings").mkdir(parents=True, exist_ok=True)
    parts, stats = [], []
    gpkg = DATA / "national" / "buildings_postgis.gpkg"
    if gpkg.exists():
        gpkg.unlink()
    for off in range(0, n_total, CHUNK):
        g = pyogrio.read_dataframe(SRC, skip_features=off, max_features=CHUNK,
                                   columns=["osm_id", "building", "building:levels", "name"])
        g = g[g.geometry.notna() & ~g.geometry.is_empty]
        g = g.rename(columns={"building:levels": "levels"})
        g["levels"] = pd.to_numeric(g["levels"], errors="coerce").astype("Float32")
        g3035 = g.geometry.to_crs(3035)
        g["footprint_m2"] = g3035.area.round(1).astype("float32")
        cen = g3035.centroid.to_crs(4326)
        x, y = to3857.transform(cen.x.values, cen.y.values)
        for k, s in samplers.items():
            g[k] = s(np.asarray(x), np.asarray(y))
        pts = gpd.GeoDataFrame({"i": np.arange(len(g))}, geometry=cen.values, crs=4326)
        j = gpd.sjoin(pts, adm3, how="left", predicate="within").drop_duplicates("i").sort_values("i")
        g["pcode"] = j["pcode"].values
        g["residential"] = g["building"].isin(RESIDENTIAL)
        g.to_parquet(LAKE / "buildings" / f"part-{off // CHUNK:03d}.parquet", index=False)
        g.to_file(gpkg, layer="buildings", driver="GPKG", append=gpkg.exists())
        hf, hc, ff = g["hand_fab_dm"].astype(int), g["hand_cop_dm"].astype(int), g["ffpi_x10"].astype(int)
        agg = pd.DataFrame({
            "pcode": g["pcode"], "n_buildings": 1, "footprint_m2": g["footprint_m2"], "n_residential": g["residential"].astype(int),
            **{f"n_hand_le_{L}m": ((hf <= 10 * L) & (hf != 255)).astype(int) for L in (1, 2, 3, 5, 10)},
            **{f"n_hand_cop_le_{L}m": ((hc <= 10 * L) & (hc != 255)).astype(int) for L in (2, 5)},
            "n_ffpi_high": (ff >= 70).astype(int), "n_ffpi_very_high": (ff >= 85).astype(int),
        }).groupby("pcode", dropna=True).sum()
        stats.append(agg)
        print(f"  {off + len(g):,}/{n_total:,}  {round(time.time() - t0)} s", flush=True)
    s = pd.concat(stats).groupby(level=0).sum()
    s["footprint_km2"] = (s.pop("footprint_m2") / 1e6).round(4)
    for L in (1, 2, 3, 5, 10):
        s[f"share_hand_le_{L}m"] = (s[f"n_hand_le_{L}m"] / s["n_buildings"]).round(4)
    s["share_ffpi_high"] = (s["n_ffpi_high"] / s["n_buildings"]).round(4)
    meta = gpd.read_parquet(ADM3).drop(columns="geometry")
    s = meta.merge(s.reset_index().rename(columns={"index": "pcode"}), on="pcode", how="left")
    LAKE.joinpath("buildings_hromadas").mkdir(parents=True, exist_ok=True)
    s.to_parquet(LAKE / "buildings_hromadas" / "stats.parquet", index=False)
    manifest = {
        "product": "exposure", "version": "v1", "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": {"buildings": "HOT OSM export of Ukraine 2025-01-11 (ODbL), 6.8 M polygons",
                    "hand": "national HAND 30 m (FABDEM, Copernicus GLO-30), display COG in decimetres",
                    "ffpi": "FFPI v2 30 m (FABDEM), not validated", "admin": "COD-AB Ukraine v05 2025-09-01 ADM3"},
        "method": "terrain values at the building centroid; HAND mask of level L: hand_dm <= 10 L (screening, not a flood depth)",
        "counts": {"buildings": int(s["n_buildings"].sum()), "hromadas": int(s["n_buildings"].notna().sum())},
    }
    (LAKE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    print("done", round(time.time() - t0), "s;", manifest["counts"], flush=True)
    print(s.nlargest(8, "n_hand_le_2m")[["name_ua", "adm1_ua", "n_buildings", "n_hand_le_2m", "share_hand_le_2m"]].to_string(index=False))


if __name__ == "__main__":
    main()
