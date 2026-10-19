"""
CSV files for the Recovery Lab tables in PostgreSQL (data_dnipro_h_q deploy/server/impact/impact_tables.sql):
  runs.csv            one row per FIAT run (region, A_MIN, levels, hazard / exposure / model, price note)
  building_damage.csv one row per damaged building and level: osm_id, class, pcode, depth, damage € 2010, centroid
  hromada_damage.csv  per run, hromada and level: buildings damaged, damage in the three price variants
  price_index.csv / price_factors.csv  as price_factors.py writes them
into <run dir>/pg/.
  ~/repo/data_dnipro_h_q/.venv-linux/bin/python scripts/national/recovery/export_pg.py [--run UA26_a50]
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import duckdb
import geopandas as gpd
import pandas as pd

from recovery_fiat import VALIDATION_NOTE

# the confidence level of a run (owner's review 2026-10-19): 1 screening, 2 hydraulic model unvalidated, 3 validated;
# 4 (validated + uncertainty + economics) is set by hand when such a study exists
CONFIDENCE = {("hand_screening", "not_validated"): 1, ("gr4j", "not_validated"): 1, ("hecras", "not_validated"): 2, ("hecras", "validated"): 3}

HERE = Path(__file__).resolve().parent
EAD_COLS = ["run", "pcode", "name", "post_id", "gauge_name", "gauge_how", "gauge_dist_km", "same_basin", "onset_m", "rise_10pct_m", "rise_1pct_m",
            "rise_02pct_m", "rises_T", "aep_by_level", "ead_eur2010", "ead_low_eur2010", "ead_high_eur2010", "ead_eur_eurostat", "ead_uah_sssu"]
MEASURE_COLS = ["run", "pcode", "measure", "design", "design_aep", "height_m", "bank_m", "height_struct_m", "protects_levels_le_m", "buildings_protected",
                "length_km", "volume_m3", "cost_low_eur", "cost_mid_eur", "cost_high_eur", "cost_wall_eur", "cost_benchmark_uah",
                "avoided_ead_eur2010", "residual_ead_eur2010", "avoided_ead_eur_eurostat", "avoided_ead_uah_sssu"]
LAKE = Path.home() / "projects/data/s3_staging/hydro/lake"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="UA26_a50")
    a = ap.parse_args()
    run_dir = LAKE / "impact/v1/fiat" / a.run
    out = run_dir / "pg"
    out.mkdir(exist_ok=True)
    summ = json.loads((run_dir / "summary.json").read_text())
    dmg = pd.read_parquet(run_dir / "damage.parquet")
    levels = sorted(int(c.split("_L")[1]) for c in dmg.columns if c.startswith("damage_L"))
    ead_meta = json.loads((run_dir / "summary_ead.json").read_text()) if (run_dir / "summary_ead.json").exists() else {}
    # centroids of the damaged buildings
    ids = set(dmg.loc[dmg[[f"damage_L{lv}" for lv in levels]].gt(0).any(axis=1), "osm_id"])
    con = duckdb.connect(); con.sql("INSTALL spatial; LOAD spatial;")
    cen = con.sql(f"SELECT osm_id, ST_X(ST_Centroid(geometry)) AS lon, ST_Y(ST_Centroid(geometry)) AS lat, footprint_m2 "
                  f"FROM read_parquet('{LAKE / 'exposure/v1/buildings'}/*.parquet') WHERE pcode LIKE '{summ['region']}%'").df()
    con.close()
    cen = cen[cen.osm_id.isin(ids)].drop_duplicates("osm_id").set_index("osm_id")
    rows = []
    for lv in levels:
        d = dmg[dmg[f"damage_L{lv}"] > 0]
        rows.append(pd.DataFrame({"run": a.run, "level_m": lv, "osm_id": d.osm_id, "class": d.cls, "pcode": d.pcode,
                                  "depth_m": d.get(f"depth_L{lv}"), "damage_eur2010": d[f"damage_L{lv}"].round(0)}))
    b = pd.concat(rows, ignore_index=True).join(cen, on="osm_id")
    b["footprint_m2"] = b["footprint_m2"].round(1)
    b[["lon", "lat"]] = b[["lon", "lat"]].round(6)
    b.to_csv(out / "building_damage.csv", index=False)
    # hromadas × levels × price variants
    fac = pd.read_csv(HERE / "price_factors.csv")
    f = {(r.variant, r["class"]): r.factor for _, r in fac.iterrows()}
    b["eur_eurostat"] = b.damage_eur2010 * b["class"].map(lambda c: f[("eur_eurostat", c)])
    b["uah_sssu"] = b.damage_eur2010 * b["class"].map(lambda c: f[("uah_sssu", c)])
    names = gpd.read_parquet(LAKE / "admin/v1/adm3.parquet").set_index("pcode")["name_ua"]
    h = b.groupby(["run", "pcode", "level_m"]).agg(buildings_damaged=("osm_id", "count"), damage_eur2010=("damage_eur2010", "sum"),
                                                   damage_eur_eurostat=("eur_eurostat", "sum"), damage_uah_sssu=("uah_sssu", "sum")).round(0).reset_index()
    h.insert(2, "name", h.pcode.map(names))
    h.to_csv(out / "hromada_damage.csv", index=False)
    yr = {r.variant: int(r.price_year) for _, r in fac.iterrows()}
    pd.DataFrame([{"run": a.run, "region": summ["region"], "a_min_km2": summ["a_min_km2"], "levels": "{" + ",".join(map(str, levels)) + "}",
                   "buildings": summ["buildings"], "ground_floor_m": summ["ground_floor_m"],
                   "hazard": "water depth = L − HAND (FABDEM 30 m) above rivers ≥ A_MIN km²; screening, not hydraulics",
                   "exposure": "OSM buildings (exposure/v1/buildings): RES / COM / IND by tag, footprint",
                   "model": "Delft-FIAT 0.2.1, JRC European damage functions, JRC Ukraine max damage (Huizinga et al. 2017)",
                   "price_eurostat_year": yr["eur_eurostat"], "price_sssu_year": yr["uah_sssu"],
                   "scenario_kind": summ.get("scenario_kind", "hand_level_m"), "hazard_kind": summ.get("hazard_kind", "hand_screening"),
                   "hazard_label": summ.get("hazard_label", "HAND screening"), "validation": summ.get("validation", "not_validated"),
                   "validation_note": summ.get("validation_note") or VALIDATION_NOTE.get(summ.get("hazard_kind", "hand_screening"), ""),
                   "ead_method": ead_meta.get("method"),
                   "confidence_level": CONFIDENCE.get((summ.get("hazard_kind", "hand_screening"), summ.get("validation", "not_validated")), 1)}]).to_csv(out / "runs.csv", index=False)
    # the EAD and measures tables are always there (empty with headers when recovery_ead.py has not run), so one SQL loads a run
    for name, cols in (("hromada_ead.csv", EAD_COLS), ("measures.csv", MEASURE_COLS)):
        if not (out / name).exists():
            pd.DataFrame(columns=cols).to_csv(out / name, index=False)
    for n in ("price_index.csv", "price_factors.csv"):
        shutil.copy(HERE / n, out / n)
    print(f"{out}: {len(b):,} building rows, {len(h):,} hromada rows")


if __name__ == "__main__":
    main()
