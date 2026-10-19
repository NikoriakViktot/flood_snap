"""
Recovery Lab pilot: direct flood damage to buildings with Delft-FIAT (Deltares) and the JRC global depth-damage functions
(Huizinga et al. 2017, EUR 28552 EN, doi:10.2760/16510) — owner, 2026-10-19: «Recovery Lab обчислює можливі прямі
економічні збитки через Delft-FIAT». Pilot: Ivano-Frankivsk oblast (UA26), the same scenarios as evacuation.py.

Hazard per level L (1 / 2 / 3 / 5 m): water depth = L − HAND where HAND ≤ L above a river of ≥ A_MIN km² (stream_area.py),
on a 30 m EPSG:3035 grid over the region. Exposure: every OSM building of the region (exposure/v1/buildings), its footprint
as the damaged floor area, class by its OSM tag:
  RES  houses, apartments, `yes` and other untyped buildings (in villages mostly houses and farm buildings — an upper bound)
  COM  shops, offices, hotels, schools, hospitals, churches and other public buildings
  IND  industrial, warehouses, factories
  skipped: roof, ruins, construction, storage tanks
Ground floor 0.3 m above the ground. Max damage = JRC Ukraine building-based values (€/m², 2010 prices: structure +
content), JRC European damage functions. FIAT extracts the depth over each footprint (mean).

Outputs: data/s3_staging/hydro/lake/impact/v1/fiat/<region>_a<A>/
  L<l>/ (FIAT run: settings.toml, output.csv, …), damage.parquet (object_id, osm_id, class, pcode, damage per level),
  summary.json (per level and per hromada: buildings damaged, € structure / content / total)

  ~/repo/data_dnipro_h_q/.venv-linux/bin/python scripts/national/recovery/recovery_fiat.py [--region UA26] [--a-min 50]

Hydraulic depths instead of the screening (owner, 2026-10-19: «замінити скринінгову глибину на гідравлічну там, де є
HEC-RAS або GR4J»): --hazard-tif T=<depth.tif> per return period T (years), e.g. the max-depth rasters of an imported
HEC-RAS run (Django apps/hecras, Terracotta <tc_category>/depth_max) or a GR4J → HAND depth product. The scenario key is
then the return period, `scenario_kind = return_period_years`; --hazard-kind hecras|gr4j and --validation
validated|not_validated are written to the run so the page can say what the numbers rest on. Nothing is validated
until a hydraulic model has been checked against observed floods; the HAND screening never is.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import duckdb
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.warp import Resampling, reproject
from rasterio.transform import from_origin

HERE = Path(__file__).resolve().parent
DATA = Path.home() / "projects/data"
LAKE = DATA / "s3_staging/hydro/lake"
HAND_COG = DATA / "national/hand_ua/fab_dem_hand_cog.tif"
AREA_DIR = DATA / "national/stream_area"
ADM3 = LAKE / "admin/v1/adm3.parquet"
LEVELS = (1, 2, 3, 5)
GROUND_FLOOR_M = 0.3
IMAGE = "geohydroai-fiat:0.2"
IND = {"industrial", "warehouse", "factory", "manufacture", "storage", "hangar"}
COM = {"commercial", "retail", "office", "hotel", "kiosk", "supermarket", "service", "school", "kindergarten", "hospital",
       "clinic", "church", "chapel", "cathedral", "civic", "public", "government", "university", "college", "train_station",
       "fire_station", "museum", "sports_hall", "stadium"}
SKIP = {"roof", "ruins", "construction", "storage_tank", "bunker", "collapsed", "demolished"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def depth_grids(region_3035, a_min: float, out: Path) -> dict[int, Path]:
    minx, miny, maxx, maxy = region_3035.bounds
    res = 30.0
    w, h = int(np.ceil((maxx - minx) / res)), int(np.ceil((maxy - miny) / res))
    tf = from_origin(minx, maxy, res, res)
    hand = np.full((h, w), 255, np.uint8)
    with rasterio.open(HAND_COG) as r:
        reproject(rasterio.band(r, 1), hand, dst_transform=tf, dst_crs="EPSG:3035", resampling=Resampling.nearest, dst_nodata=255)
    area = np.full((h, w), np.nan, np.float32)
    for f in sorted(AREA_DIR.glob("*.tif")):
        if f.name.endswith("_acc.tif"):
            continue
        tmp = np.full((h, w), np.nan, np.float32)
        with rasterio.open(f) as r:
            reproject(rasterio.band(r, 1), tmp, dst_transform=tf, dst_crs="EPSG:3035", resampling=Resampling.nearest, dst_nodata=np.nan)
        area = np.where(np.isnan(area) & np.isfinite(tmp), tmp, area)
    hm = np.where(hand == 255, np.inf, hand / 10.0)
    paths = {}
    prof = {"driver": "GTiff", "dtype": "float32", "width": w, "height": h, "count": 1, "crs": "EPSG:3035", "transform": tf,
            "nodata": -9999.0, "compress": "deflate", "tiled": True, "blockxsize": 512, "blockysize": 512}
    for lv in LEVELS:
        d = np.where((hm <= lv) & (area >= a_min), lv - hm, -9999.0).astype(np.float32)
        p = out / f"depth_L{lv}.tif"
        with rasterio.open(p, "w", **prof) as o:
            o.write(d, 1)
        paths[lv] = p
        log(f"depth L={lv} m: {(d > 0).sum() * 900 / 1e6:.1f} km² under water")
    return paths


def external_depth_grids(region_3035, tifs: dict[int, Path], out: Path) -> dict[int, Path]:
    """hydraulic depth rasters (any CRS / resolution) resampled onto the region's 30 m EPSG:3035 grid; ≤ 0 = dry"""
    minx, miny, maxx, maxy = region_3035.bounds
    res = 30.0
    w, h = int(np.ceil((maxx - minx) / res)), int(np.ceil((maxy - miny) / res))
    tf = from_origin(minx, maxy, res, res)
    prof = {"driver": "GTiff", "dtype": "float32", "width": w, "height": h, "count": 1, "crs": "EPSG:3035", "transform": tf,
            "nodata": -9999.0, "compress": "deflate", "tiled": True, "blockxsize": 512, "blockysize": 512}
    paths = {}
    for key, src in tifs.items():
        d = np.full((h, w), -9999.0, np.float32)
        with rasterio.open(src) as r:
            reproject(rasterio.band(r, 1), d, dst_transform=tf, dst_crs="EPSG:3035", resampling=Resampling.bilinear, dst_nodata=-9999.0)
        d = np.where(np.isfinite(d) & (d > 0), d, -9999.0).astype(np.float32)
        p = out / f"depth_L{key}.tif"
        with rasterio.open(p, "w", **prof) as o:
            o.write(d, 1)
        paths[key] = p
        log(f"depth T={key} y from {src.name}: {(d > 0).sum() * 900 / 1e6:.1f} km² under water")
    return paths


VALIDATION_NOTE = {
    "hand_screening": "Не змодельовано гідравлічно: глибина = рівень − висота над руслом (HAND). Оціночні дані, не валідовані за "
                      "спостереженими паводками. Точні глибини дає лише гідравлічна модель (HEC-RAS); для регіону її ще немає.",
    "hecras": "Глибини з гідравлічної моделі HEC-RAS. Валідованими є лише прогони, перевірені за спостереженими паводками "
              "(статус прогону в платформі).",
    "gr4j": "Глибини з ланцюга GR4J → крива HAND: гідрологічна модель, без гідравліки русла. Оціночні дані, не валідовані.",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="UA26")
    ap.add_argument("--a-min", type=float, default=50.0)
    ap.add_argument("--hazard-tif", action="append", default=[], metavar="T=PATH",
                    help="hydraulic depth raster of the T-year flood instead of the HAND screening (repeatable)")
    ap.add_argument("--hazard-kind", choices=list(VALIDATION_NOTE), default=None, help="hand_screening (default) | hecras | gr4j")
    ap.add_argument("--hazard-label", default="", help="e.g. 'HEC-RAS M5.3-R013 Яблуниця — гирло, прогін 2027-03'")
    ap.add_argument("--validation", choices=["not_validated", "validated"], default="not_validated")
    ap.add_argument("--tag", default="", help="run name suffix for an external hazard (default: the hazard kind)")
    a = ap.parse_args()
    t0 = time.time()
    tifs = {int(k): Path(v) for k, v in (x.split("=", 1) for x in a.hazard_tif)}
    hazard_kind = a.hazard_kind or ("hecras" if tifs else "hand_screening")
    if tifs and hazard_kind == "hand_screening":
        raise SystemExit("--hazard-tif needs --hazard-kind hecras or gr4j")
    if not tifs and a.validation == "validated":
        raise SystemExit("the HAND screening is never validated")
    scenario_kind = "return_period_years" if tifs else "hand_level_m"
    keys = sorted(tifs) if tifs else list(LEVELS)
    out = LAKE / "impact/v1/fiat" / (f"{a.region}_{a.tag or hazard_kind}" if tifs else f"{a.region}_a{a.a_min:g}")
    out.mkdir(parents=True, exist_ok=True)
    adm3 = gpd.read_parquet(ADM3)
    reg = adm3[adm3["pcode"].str.startswith(a.region)]
    region = reg.to_crs(3035).union_all()
    # vulnerability: JRC European functions, FIAT csv
    f = pd.read_csv(HERE / "jrc_europe_damage_functions.csv")
    vul = out / "vulnerability.csv"
    with vul.open("w") as v:
        v.write("#UNIT=meter\n#method,mean,mean,mean\nwater depth,RES,COM,IND\n")
        for _, r in f.iterrows():
            v.write(f"{r.depth_m},{r['Residential buildings']},{r['Commercial buildings']},{r['Industrial buildings']}\n")
    md = pd.read_csv(HERE / "jrc_ukraine_max_damage_2010.csv").set_index("class")
    key = {"RES": "Residential", "COM": "Commercial", "IND": "Industrial"}
    # exposure
    bpath = LAKE / "exposure/v1/buildings"
    con = duckdb.connect(); con.sql("INSTALL spatial; LOAD spatial;")
    b = con.sql(f"SELECT osm_id, building, residential, footprint_m2, pcode, ST_AsWKB(geometry) AS wkb FROM read_parquet('{bpath}/*.parquet') "
                f"WHERE pcode LIKE '{a.region}%'").df()
    con.close()
    b = gpd.GeoDataFrame(b.drop(columns="wkb"), geometry=gpd.GeoSeries.from_wkb(b["wkb"].map(bytes)), crs=4326)
    tag = b["building"].fillna("yes").str.lower()
    b = b[~tag.isin(SKIP)].copy()
    tag = b["building"].fillna("yes").str.lower()
    b["cls"] = np.where(tag.isin(IND), "IND", np.where(tag.isin(COM), "COM", "RES"))
    b = b.to_crs(3035).reset_index(drop=True)
    b["object_id"] = np.arange(1, len(b) + 1)
    area = b.geometry.area
    b["max_damage_structure"] = (area * b["cls"].map(lambda c: md.at[key[c], "structure_eur_m2"])).round(0)
    b["max_damage_content"] = (area * b["cls"].map(lambda c: md.at[key[c], "content_eur_m2"])).round(0)
    expo = out / "exposure"
    expo.mkdir(exist_ok=True)
    b[["object_id", "geometry"]].to_file(expo / "buildings.gpkg", driver="GPKG")
    pd.DataFrame({"object_id": b["object_id"], "extract_method": "area", "ground_flht": GROUND_FLOOR_M, "ground_elevtn": 0,
                  "fn_damage_structure": b["cls"], "max_damage_structure": b["max_damage_structure"],
                  "fn_damage_content": b["cls"], "max_damage_content": b["max_damage_content"]}).to_csv(expo / "buildings.csv", index=False)
    log(f"exposure: {len(b):,} buildings ({b.cls.value_counts().to_dict()}), "
        f"max damage {(b.max_damage_structure.sum() + b.max_damage_content.sum()) / 1e6:,.0f} M€ (2010)")
    hz = external_depth_grids(region, tifs, out) if tifs else depth_grids(region, a.a_min, out)
    res = b[["object_id", "osm_id", "cls", "pcode"]].copy()
    for lv in keys:
        run = out / f"L{lv}"
        run.mkdir(exist_ok=True)
        (run / "settings.toml").write_text(f'''[global]
crs = "EPSG:3035"

[output]
path = "/m/L{lv}/output"
[output.csv]
name = "output.csv"
[output.geom]
name1 = "buildings.gpkg"

[hazard]
file = "/m/{hz[lv].name}"
crs = "EPSG:3035"
risk = false
elevation_reference = "DEM"

[exposure.csv]
file = "/m/exposure/buildings.csv"
[exposure.geom]
file1 = "/m/exposure/buildings.gpkg"
crs = "EPSG:3035"

[vulnerability]
file = "/m/vulnerability.csv"
step_size = 0.01
''')
        t = time.time()
        r = subprocess.run(["docker", "run", "--rm", "--user", f"{subprocess.check_output(['id', '-u']).decode().strip()}:{subprocess.check_output(['id', '-g']).decode().strip()}",
                            "-v", f"{out}:/m", IMAGE, "run", f"/m/L{lv}/settings.toml"], capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"FIAT L={lv} failed:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
        o = pd.read_csv(run / "output" / "output.csv")
        dcol = [c for c in o.columns if c.lower().startswith("total_damage") or c.lower() == "total damage"]
        scol = [c for c in o.columns if "damage_structure" in c.lower() and not c.lower().startswith("max")]
        ccol = [c for c in o.columns if "damage_content" in c.lower() and not c.lower().startswith("max")]
        wcol = [c for c in o.columns if "inun_depth" in c.lower() or c.lower().startswith("inundation")]
        o = o.set_index("object_id")
        res[f"damage_L{lv}"] = res["object_id"].map(o[dcol[0]] if dcol else o[scol[0]] + o[ccol[0]]).fillna(0)
        if wcol:
            res[f"depth_L{lv}"] = res["object_id"].map(o[wcol[0]])
        log(f"FIAT {'T' if tifs else 'L'}={lv}: {time.time() - t:.0f} s, damaged {(res[f'damage_L{lv}'] > 0).sum():,} buildings, "
            f"{res[f'damage_L{lv}'].sum() / 1e6:,.1f} M€ (2010)  [columns {list(o.columns)[:12]}]")
    res.to_parquet(out / "damage.parquet", index=False)
    names = adm3.set_index("pcode")["name_ua"]
    summ = {"region": a.region, "a_min_km2": None if tifs else a.a_min, "price_level": "EUR 2010 (JRC)", "ground_floor_m": GROUND_FLOOR_M,
            "buildings": int(len(b)), "classes": b.cls.value_counts().to_dict(), "scenario_kind": scenario_kind, "scenario_keys": keys,
            "hazard_kind": hazard_kind, "hazard_label": a.hazard_label or ("HAND screening" if not tifs else hazard_kind),
            "hazard_files": {str(k): str(v) for k, v in tifs.items()}, "validation": a.validation,
            "validation_note": VALIDATION_NOTE[hazard_kind], "levels": {}}
    for lv in keys:
        dmg = res[f"damage_L{lv}"]
        per = res[dmg > 0].groupby("pcode")[f"damage_L{lv}"].agg(["count", "sum"]).sort_values("sum", ascending=False)
        summ["levels"][str(lv)] = {"damaged": int((dmg > 0).sum()), "damage_eur": round(float(dmg.sum())),
                                   "by_class_eur": res.groupby("cls")[f"damage_L{lv}"].sum().round().to_dict(),
                                   "hromadas": [{"pcode": p, "name": names.get(p), "damaged": int(r["count"]), "damage_eur": round(float(r["sum"]))}
                                                for p, r in per.iterrows()]}
    (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1))
    log(f"done in {time.time() - t0:.0f} s → {out}")


if __name__ == "__main__":
    main()
