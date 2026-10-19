"""
Recovery Lab, step 2 (owner, 2026-10-19): expected annual damage (EAD) and the protection measures of every hromada,
on top of a recovery_fiat.py run (damage per building at the screening levels 1 / 2 / 3 / 5 m above the river).

Levels → probabilities. Each hromada takes a GAUGE: the gauge inside it with the largest catchment (its main river),
else the nearest gauge with a flood study (status ok, a river post) within MAX_GAUGE_KM of its damaged buildings. The
gauge's flood study (Hydro flood_scenarios: the stage rise above low water of the 10 % / 1 % / 0.2 % annual floods, at-site
frequency analysis of the yearbook maxima) gives three (rise, AEP) points; ln AEP is interpolated linearly in the rise,
extrapolated with the neighbouring slope below the 10 % rise (capped at AEP_MAX, the bankfull flood) and above the 0.2 %
rise (floored at AEP_MIN). A HAND level L is read as a stage rise of L m, i.e. the flood plain floods uniformly with the
gauge: a screening, not a hydraulic model.

EAD = ∫ D dP by the trapezoid rule over the levels, plus a triangle from the first level down to AEP(0) (damage grows
from 0) and the damage of the highest level held constant for rarer floods (a lower bound of the tail).

Measures (per hromada, avoided EAD = EAD − EAD with the measure):
  dike      a dike / embankment that holds every flood up to its design level: height = the gauge's rise of the design
            flood + FREEBOARD_M; three designs (10 %, 1 %, 0.2 %) so the trade-off height ↔ avoided damage is visible.
            Overtopping only (levels above the design flood do their full damage); no breach, no residual seepage.
            Length = the river bank that fronts the damaged buildings: river lines of the hromada within DIKE_REACH_M of
            a building damaged at the 5 m level. Cost needs a unit rate the owner sets (measures_params.json): null until then.
  floor     raising the ground floor of the residential buildings in the zone by 0.5 / 1.0 m (new construction, major
            repair): FIAT re-run with ground_flht = 0.3 + h for RES, COM / IND unchanged.
Validation: none of this is validated. The hazard is a HAND screening; only a hydraulic model (HEC-RAS) gives depths
that can be checked against observed floods, and none exists for the region yet.

Outputs in the run's pg/: hromada_ead.csv, measures.csv, gauge_links.csv; summary_ead.json beside summary.json.
  ~/repo/data_dnipro_h_q/.venv-linux/bin/python scripts/national/recovery/recovery_ead.py --run UA26_a50
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from recovery_fiat import GROUND_FLOOR_M, IMAGE, LAKE, LEVELS, log

HERE = Path(__file__).resolve().parent
SERVING = sorted((Path.home() / "repo/data_dnipro_h_q/data/serving").glob("2*"))[-1]
SCEN = SERVING / "flood_scenarios.parquet"
RIVERS = SERVING / "river_system_lines.geoparquet"
ADM3 = LAKE / "admin/v1/adm3.parquet"
AEPS = {"high": 0.1, "medium": 0.01, "low": 0.002}
AEP_MAX, AEP_MIN = 0.5, 1e-4
MAX_GAUGE_KM = 80.0
FREEBOARD_M = 0.5
DIKE_REACH_M = 300.0
FLOOR_RAISE_M = (0.5, 1.0)


def aep_of_level(rises: dict[str, float]):
    """ln AEP linear in the stage rise between the three study points; the neighbouring slope beyond them"""
    xs = np.array([rises["high"], rises["medium"], rises["low"]])
    ys = np.log(np.array([AEPS["high"], AEPS["medium"], AEPS["low"]]))
    if not (xs[0] < xs[1] < xs[2]):
        return None

    def f(level: float) -> float:
        if level <= xs[0]:
            y = ys[0] + (ys[1] - ys[0]) / (xs[1] - xs[0]) * (level - xs[0])
        elif level >= xs[2]:
            y = ys[2] + (ys[2] - ys[1]) / (xs[2] - xs[1]) * (level - xs[2])
        else:
            y = float(np.interp(level, xs, ys))
        return float(min(AEP_MAX, max(AEP_MIN, np.exp(y))))
    return f


def ead(levels: list[float], damages: list[float], aep) -> float:
    """∫ D dP: triangle from AEP(0) to the first level, trapezoids between levels, the last damage held for rarer floods"""
    p = [aep(lv) for lv in levels]
    p0 = aep(0.0)
    total = max(p0 - p[0], 0) * damages[0] / 2
    for i in range(len(levels) - 1):
        total += max(p[i] - p[i + 1], 0) * (damages[i] + damages[i + 1]) / 2
    total += p[-1] * damages[-1]
    return total


def fiat_run(out: Path, tag: str, lv: int, exposure_csv: str) -> pd.Series:
    """one FIAT run on the run's depth grid of level lv with another exposure csv; damage per object_id (€ 2010)"""
    run = out / "measures" / tag / f"L{lv}"
    run.mkdir(parents=True, exist_ok=True)
    (run / "settings.toml").write_text(f'''[global]
crs = "EPSG:3035"

[output]
path = "/m/measures/{tag}/L{lv}/output"
[output.csv]
name = "output.csv"
[output.geom]
name1 = "buildings.gpkg"

[hazard]
file = "/m/depth_L{lv}.tif"
crs = "EPSG:3035"
risk = false
elevation_reference = "DEM"

[exposure.csv]
file = "/m/exposure/{exposure_csv}"
[exposure.geom]
file1 = "/m/exposure/buildings.gpkg"
crs = "EPSG:3035"

[vulnerability]
file = "/m/vulnerability.csv"
step_size = 0.01
''')
    done = run / "output" / "output.csv"
    if done.exists() and done.stat().st_mtime > (out / f"depth_L{lv}.tif").stat().st_mtime:   # a re-run reuses FIAT's output
        return pd.read_csv(done).set_index("object_id")["total_damage"]
    uid = subprocess.check_output(["id", "-u"]).decode().strip(); gid = subprocess.check_output(["id", "-g"]).decode().strip()
    r = subprocess.run(["docker", "run", "--rm", "--user", f"{uid}:{gid}", "-v", f"{out}:/m", IMAGE, "run", f"/m/measures/{tag}/L{lv}/settings.toml"],
                       capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"FIAT {tag} L={lv} failed:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
    o = pd.read_csv(run / "output" / "output.csv").set_index("object_id")
    return o["total_damage"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="UA26_a50")
    ap.add_argument("--skip-floor", action="store_true", help="no FIAT re-runs for the floor-raising measure")
    a = ap.parse_args()
    t0 = time.time()
    out = LAKE / "impact/v1/fiat" / a.run
    summ = json.loads((out / "summary.json").read_text())
    region_code = summ["region"]
    dmg = pd.read_parquet(out / "damage.parquet")
    keys = summ.get("scenario_keys") or list(LEVELS)
    by_aep = summ.get("scenario_kind", "hand_level_m") == "return_period_years"
    levels = [float(k) for k in keys]
    adm3 = gpd.read_parquet(ADM3)
    reg = adm3[adm3["pcode"].str.startswith(region_code)].to_crs(3035)
    names = adm3.set_index("pcode")["name_ua"]
    fac = pd.read_csv(HERE / "price_factors.csv")
    factor = {(r.variant, r["class"]): r.factor for _, r in fac.iterrows()}
    # ---- gauges with a flood study
    sc = pd.read_parquet(SCEN)
    sc = sc[(sc["status"] == "ok") & (sc["station_type"] == "river") & sc[["high_depth_m", "medium_depth_m", "low_depth_m"]].notna().all(axis=1)]
    g = gpd.GeoDataFrame(sc, geometry=gpd.points_from_xy(sc["lon"], sc["lat"]), crs=4326).to_crs(3035)
    # ---- damaged buildings per hromada (centroids of the exposure)
    expo = gpd.read_file(out / "exposure" / "buildings.gpkg")[["object_id", "geometry"]]
    expo["geometry"] = expo.geometry.centroid
    b = dmg.merge(expo, on="object_id").pipe(gpd.GeoDataFrame, geometry="geometry", crs=3035)
    dcols = [f"damage_L{k}" for k in keys]
    hit = b[b[dcols].gt(0).any(axis=1)]
    # ---- gauge per hromada (a return-period hazard carries its own probabilities: no gauge, no dike)
    links = []
    for _, h in reg.iterrows():
        inside = g[g.within(h.geometry)]
        pts = hit[hit["pcode"] == h["pcode"]]
        if pts.empty:
            continue
        if by_aep:
            links.append({"pcode": h["pcode"], "name": h["name_ua"], "post_id": None, "gauge_name": summ.get("hazard_label"), "how": "hazard_aep",
                          "dist_km": None, "rise_10pct_m": None, "rise_1pct_m": None, "rise_02pct_m": None}); continue
        c = pts.geometry.union_all().centroid
        if not inside.empty:
            gg = inside.loc[inside["F_km2"].fillna(0).idxmax()]; how = "inside"; dist = gg.geometry.distance(c) / 1000
        else:
            d = g.geometry.distance(c)
            if d.min() / 1000 > MAX_GAUGE_KM:
                links.append({"pcode": h["pcode"], "name": h["name_ua"], "post_id": None, "how": "none"}); continue
            gg = g.loc[d.idxmin()]; how = "nearest"; dist = d.min() / 1000
        links.append({"pcode": h["pcode"], "name": h["name_ua"], "post_id": int(gg["post_id"]), "gauge_name": gg["post_name_ua"],
                      "river": gg["river"], "F_km2": gg["F_km2"], "how": how, "dist_km": round(float(dist), 1),
                      "rise_10pct_m": round(float(gg["high_depth_m"]), 2), "rise_1pct_m": round(float(gg["medium_depth_m"]), 2),
                      "rise_02pct_m": round(float(gg["low_depth_m"]), 2), "record_m": gg["record_depth_m"], "n_years": int(gg["n_years"])})
    L = pd.DataFrame(links)
    log(f"gauges: {int((L.how == 'inside').sum())} hromadas with a gauge inside, {int((L.how == 'nearest').sum())} nearest, {int((L.how == 'none').sum())} none")
    # ---- floor raising: FIAT re-runs
    floor_dmg: dict[float, dict[int, pd.Series]] = {}
    if not a.skip_floor:
        base = pd.read_csv(out / "exposure" / "buildings.csv")
        cls = dmg.set_index("object_id")["cls"]
        for h in FLOOR_RAISE_M:
            e = base.copy()
            e["ground_flht"] = np.where(e["object_id"].map(cls) == "RES", GROUND_FLOOR_M + h, GROUND_FLOOR_M)
            name = f"buildings_floor{int(h * 100):03d}.csv"
            e.to_csv(out / "exposure" / name, index=False)
            floor_dmg[h] = {}
            for lv in keys:
                t = time.time()
                floor_dmg[h][lv] = fiat_run(out, f"floor{int(h * 100):03d}", lv, name)
                log(f"floor +{h} m, L={lv}: {time.time() - t:.0f} s, damage {floor_dmg[h][lv].sum() / 1e6:,.1f} M€ (was {dmg[f'damage_L{lv}'].sum() / 1e6:,.1f})")
    # ---- river lines for the dike length
    riv = gpd.read_parquet(RIVERS).to_crs(3035)
    riv = riv[riv.intersects(reg.union_all())]
    # ---- EAD and measures per hromada
    rows, meas = [], []
    for _, lk in L.iterrows():
        if lk["how"] == "none":
            continue
        pc = lk["pcode"]
        f = (lambda k: AEP_MAX if k <= 0 else 1.0 / k) if by_aep else \
            aep_of_level({"high": lk["rise_10pct_m"], "medium": lk["rise_1pct_m"], "low": lk["rise_02pct_m"]})
        if f is None:
            continue
        hb = b[b["pcode"] == pc]
        D = [float(hb[c].sum()) for c in dcols]
        by_cls = {cl: [float(hb.loc[hb.cls == cl, c].sum()) for c in dcols] for cl in ("RES", "COM", "IND")}

        def priced(ds_by_cls, variant):
            return sum(ead(levels, ds, f) * factor[(variant, cl)] for cl, ds in ds_by_cls.items())
        e0 = ead(levels, D, f)
        row = {"run": a.run, "pcode": pc, "name": lk["name"], "post_id": lk["post_id"], "gauge_name": lk["gauge_name"], "gauge_how": lk["how"],
               "gauge_dist_km": lk["dist_km"], "rise_10pct_m": lk["rise_10pct_m"], "rise_1pct_m": lk["rise_1pct_m"], "rise_02pct_m": lk["rise_02pct_m"],
               "aep_by_level": json.dumps({str(k): round(f(float(k)), 4) for k in keys}),
               "ead_eur2010": round(e0), "ead_eur_eurostat": round(priced(by_cls, "eur_eurostat")), "ead_uah_sssu": round(priced(by_cls, "uah_sssu"))}
        rows.append(row)
        # dike designs
        hit_h = hit[hit["pcode"] == pc]
        bank = riv[riv.intersects(reg.loc[reg.pcode == pc, "geometry"].iloc[0])]
        near = bank.intersection(hit_h.geometry.buffer(DIKE_REACH_M).union_all()) if len(hit_h) else gpd.GeoSeries([], crs=3035)
        length_km = float(shapely.length(near.union_all()) / 1000) if len(near) else 0.0
        for label, aep_d in (() if by_aep else (("10pct", 0.1), ("1pct", 0.01), ("02pct", 0.002))):
            H = lk[f"rise_{label}_m"] + FREEBOARD_M
            kept = {cl: [d if lv > H else 0.0 for lv, d in zip(levels, ds)] for cl, ds in by_cls.items()}
            Dk = [sum(kept[cl][i] for cl in kept) for i in range(len(levels))]
            e1 = ead(levels, Dk, f)
            protected = int((hb[[c for c, lv in zip(dcols, levels) if lv <= H]].gt(0).any(axis=1)).sum()) if any(lv <= H for lv in levels) else 0
            meas.append({"run": a.run, "pcode": pc, "measure": "dike", "design": label, "design_aep": aep_d, "height_m": round(H, 2),
                         "protects_levels_le_m": max([lv for lv in levels if lv <= H], default=0), "buildings_protected": protected,
                         "length_km": round(length_km, 2), "unit_cost_eur_per_km_m": None, "cost_eur": None,
                         "avoided_ead_eur2010": round(e0 - e1), "residual_ead_eur2010": round(e1),
                         "avoided_ead_eur_eurostat": round(priced(by_cls, "eur_eurostat") - priced(kept, "eur_eurostat")),
                         "avoided_ead_uah_sssu": round(priced(by_cls, "uah_sssu") - priced(kept, "uah_sssu"))})
        for h, per_lv in floor_dmg.items():
            ids = hb["object_id"]
            raised = {cl: [float(per_lv[lv].reindex(ids[hb.cls == cl]).fillna(0).sum()) for lv in keys] for cl in ("RES", "COM", "IND")}
            Dr = [sum(raised[cl][i] for cl in raised) for i in range(len(levels))]
            e1 = ead(levels, Dr, f)
            meas.append({"run": a.run, "pcode": pc, "measure": "floor", "design": f"+{h:g} m", "design_aep": None, "height_m": h,
                         "protects_levels_le_m": None, "buildings_protected": int((hb.cls == "RES").sum() and (hb.loc[hb.cls == "RES", dcols].gt(0).any(axis=1)).sum()),
                         "length_km": None, "unit_cost_eur_per_km_m": None, "cost_eur": None,
                         "avoided_ead_eur2010": round(e0 - e1), "residual_ead_eur2010": round(e1),
                         "avoided_ead_eur_eurostat": round(priced(by_cls, "eur_eurostat") - priced(raised, "eur_eurostat")),
                         "avoided_ead_uah_sssu": round(priced(by_cls, "uah_sssu") - priced(raised, "uah_sssu"))})
    E = pd.DataFrame(rows).sort_values("ead_eur2010", ascending=False)
    M = pd.DataFrame(meas)
    pg = out / "pg"
    pg.mkdir(exist_ok=True)
    E.to_csv(pg / "hromada_ead.csv", index=False)
    M.to_csv(pg / "measures.csv", index=False)
    L.to_csv(pg / "gauge_links.csv", index=False)
    method = (f"levels → AEP by the hromada's gauge (at-site flood study, ln AEP linear in the stage rise, capped at {AEP_MAX}); "
              f"trapezoid EAD; dike = design rise + {FREEBOARD_M} m freeboard, overtopping only" if not by_aep else
              "AEP = 1 / T of the hazard scenarios; trapezoid EAD; no dike design without water-surface elevations")
    s = {"run": a.run, "method": method, "ead_eur2010": round(float(E.ead_eur2010.sum())), "ead_eur_eurostat": round(float(E.ead_eur_eurostat.sum())),
         "ead_uah_sssu": round(float(E.ead_uah_sssu.sum())), "hromadas": len(E),
         "top": E.head(8)[["pcode", "name", "gauge_name", "ead_eur2010"]].to_dict("records"),
         "measures_region": M.groupby(["measure", "design"])[["avoided_ead_eur2010", "buildings_protected"]].sum().reset_index().to_dict("records")}
    (out / "summary_ead.json").write_text(json.dumps(s, ensure_ascii=False, indent=1, default=float))
    log(f"EAD {s['ead_eur2010'] / 1e6:,.1f} M€/yr (2010) over {len(E)} hromadas; done in {time.time() - t0:.0f} s")
    print(json.dumps(s["measures_region"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
