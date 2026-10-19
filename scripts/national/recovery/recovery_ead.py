"""
Recovery Lab, step 2 (owner, 2026-10-19; revised after the owner's review the same day): expected annual damage (EAD)
and the protection measures of every hromada, on top of a recovery_fiat.py run (damage per building at the screening
levels 1 / 2 / 3 / 5 m above the river). A PRELIMINARY estimate: only hydrodynamic modelling (HEC-RAS, calibrated on
observed floods) gives a better one; nothing here is validated.

Levels → probabilities. Each hromada takes a GAUGE: the gauge inside it with the largest catchment (its main river),
else the nearest gauge with a flood study (status ok, a river post) within MAX_GAUGE_KM of its damaged buildings. The
study gives the Gumbel fit of the annual stage rise above low water (the 10 % and 1 % Gumbel quantiles → μ, β), from
which the rises of the T = 2 … 500-year floods are taken; ln AEP is piecewise linear in the rise between them (Rosbjerg
2024), capped at AEP_MAX below the 2-year rise and floored at AEP_MIN above the 500-year one. A HAND level L is read as
a stage rise of L m, i.e. the flood plain floods uniformly with the gauge.
Damage onset: where the yearbook gives the gauge's flood-plain level (flood_plain_cm), the levels below that rise do no
damage (the river stays in its channel); without it, every level counts (flagged `onset_m = null`).
Representativeness: whether the gauge lies in the same river-basin district as the hromada's damaged buildings
(5.3 Prut–Siret vs М5 Dniester), inside or nearest, and how far.
EAD = ∫ D dP by the trapezoid rule over the levels, a triangle from the first damaging level down to AEP(onset), the
damage of the highest level held constant for rarer floods. Sensitivity: ead_low (onset on, AEP_MAX 0.33) …
ead_high (no onset, AEP_MAX 0.5); the central value uses the onset where known and AEP_MAX 0.5.

Measures (per hromada, avoided EAD = EAD − EAD with the measure; an idealised potential, not a proven effect):
  dike   holds every flood up to its design level: design rise (T = 10 / 100 / 500 y) + FREEBOARD_M; overtopping only,
         no breach, no backwater, no risk transfer downstream. Length = river bank within DIKE_REACH_M of a building
         damaged at the 5 m level. Structural height = crest level − bank ground, the ground taken as the 25th
         percentile of the damaged buildings' height above the stream. Cost by the embankment volume model
         V = L (b·H + z·H²) × unit cost (measures_params.json: low / mid / high, with source and price year, as a
         sensitivity, not an estimate); a flood wall per metre as the alternative; the oblast programme's UAH/km as a benchmark.
  floor  raising the ground floor of residential buildings by 0.5 / 1.0 m (FIAT re-run, cached).

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
import rasterio
import shapely

from recovery_fiat import GROUND_FLOOR_M, IMAGE, LAKE, LEVELS, log

HERE = Path(__file__).resolve().parent
PARAMS = json.loads((HERE / "measures_params.json").read_text())
SERVING = sorted((Path.home() / "repo/data_dnipro_h_q/data/serving").glob("2*"))[-1]
SCEN = SERVING / "flood_scenarios.parquet"
RIVERS = SERVING / "river_system_lines.geoparquet"
ADM3 = LAKE / "admin/v1/adm3.parquet"
AREA_DIR = Path.home() / "projects/data/national/stream_area"
AEP_MIN = 1e-4
MAX_GAUGE_KM = 80.0
FREEBOARD_M = 0.5
DIKE_REACH_M = 300.0
FLOOR_RAISE_M = (0.5, 1.0)
DESIGNS = (("10pct", 10), ("1pct", 100), ("02pct", 500))
TS = PARAMS["ead"]["return_periods_years"]


def gumbel_rises(x10: float, x100: float, x500: float, g10: float, g100: float) -> dict[int, float] | None:
    """the T-year stage rises: through the study's CENTRAL 10 % / 1 % / 0.2 % values (its chosen distribution), linear
    in the Gumbel reduced variate between them; below T = 10 extrapolated with the slope of the study's Gumbel fit"""
    y = lambda T: -np.log(-np.log(1 - 1 / T))  # noqa: E731
    if not all(np.isfinite(v) for v in (x10, x100, x500, g10, g100)) or not (x10 < x100 < x500) or g100 <= g10:
        return None
    beta_g = (g100 - g10) / (y(100) - y(10))
    out = {}
    for T in TS:
        if T <= 10:
            out[T] = float(x10 - beta_g * (y(10) - y(T)))
        elif T <= 100:
            out[T] = float(np.interp(y(T), [y(10), y(100)], [x10, x100]))
        else:
            out[T] = float(np.interp(y(T), [y(100), y(500)], [x100, x500]))
    return out


def aep_of_level(rises: dict[int, float], aep_max: float):
    """ln AEP piecewise linear in the stage rise through the T-year rises; capped / floored beyond them"""
    xs = np.array([rises[T] for T in TS]); ys = np.log(np.array([1 / T for T in TS]))
    if not np.all(np.diff(xs) > 0):
        return None

    def f(level: float) -> float:
        if level <= xs[0]:
            return float(min(aep_max, 1 / TS[0]))
        if level >= xs[-1]:
            y0 = ys[-1] + (ys[-1] - ys[-2]) / (xs[-1] - xs[-2]) * (level - xs[-1])
            return float(max(AEP_MIN, np.exp(y0)))
        return float(min(aep_max, max(AEP_MIN, np.exp(np.interp(level, xs, ys)))))
    return f


def ead(levels: list[float], damages: list[float], aep, onset: float | None) -> float:
    """∫ D dP: the levels below the onset do no damage; a triangle from AEP(onset or 0) to the first damaging level,
    trapezoids between levels, the last damage held for rarer floods"""
    pts = [(lv, d if (onset is None or lv >= onset) else 0.0) for lv, d in zip(levels, damages)]
    p = [aep(lv) for lv, _ in pts]
    p0 = aep(onset or 0.0)
    total = max(p0 - p[0], 0) * pts[0][1] / 2
    for i in range(len(pts) - 1):
        total += max(p[i] - p[i + 1], 0) * (pts[i][1] + pts[i + 1][1]) / 2
    total += p[-1] * pts[-1][1]
    return total


def fiat_run(out: Path, tag: str, lv, exposure_csv: str) -> pd.Series:
    """one FIAT run on the run's depth grid of level lv with another exposure csv; damage per object_id (€ 2010)"""
    run = out / "measures" / tag / f"L{lv}"
    run.mkdir(parents=True, exist_ok=True)
    done = run / "output" / "output.csv"
    if done.exists() and done.stat().st_mtime > (out / f"depth_L{lv}.tif").stat().st_mtime:   # a re-run reuses FIAT's output
        return pd.read_csv(done).set_index("object_id")["total_damage"]
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
    uid = subprocess.check_output(["id", "-u"]).decode().strip(); gid = subprocess.check_output(["id", "-g"]).decode().strip()
    r = subprocess.run(["docker", "run", "--rm", "--user", f"{uid}:{gid}", "-v", f"{out}:/m", IMAGE, "run", f"/m/measures/{tag}/L{lv}/settings.toml"],
                       capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"FIAT {tag} L={lv} failed:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
    return pd.read_csv(done).set_index("object_id")["total_damage"]


def district_of(points_3035: gpd.GeoSeries) -> str | None:
    """the river-basin district whose stream_area raster covers most of the points: '5.3' (Prut–Siret) or 'М5' (Dniester)"""
    best, n_best = None, 0
    for f in sorted(AREA_DIR.glob("*.tif")):
        if f.name.endswith("_acc.tif"):
            continue
        with rasterio.open(f) as r:
            v = np.array([x[0] for x in r.sample(zip(points_3035.x, points_3035.y))])
        n = int(np.isfinite(v).sum())
        if n > n_best:
            best, n_best = f.stem, n
    return {"5_3_2": "5.3", "5_3_3": "5.3", "M5_2": "М5"}.get(best or "", best)


def eurostat_factor(from_year: int) -> tuple[float, int]:
    """× to bring € of from_year to € of the latest Eurostat EU-27 construction-cost year (price_index.csv)"""
    idx = pd.read_csv(HERE / "price_index.csv")
    eu = idx[idx.source == "eurostat"].assign(year=lambda d: d.period.astype(int)).set_index("year")["value"]
    last = int(eu.index.max())
    return float(eu[last] / eu[from_year]), last


def embankment_cost(length_km: float, h_struct: float) -> dict:
    """embankment by volume and a flood wall per metre, both brought to € of the Eurostat price year; the oblast programme in UAH 2021"""
    e = PARAMS["embankment"]
    b, z = e["crest_width_m"], e["slope_h_per_v"]
    h = max(h_struct, e["min_structural_height_m"])
    vol = length_km * 1000 * (b * h + z * h * h)
    uc = e["unit_cost_eur_m3"]
    fe, _ = eurostat_factor(e["unit_cost_price_year"])
    fw, _ = eurostat_factor(PARAMS["wall"]["unit_cost_price_year"])
    return {"volume_m3": round(vol), "cost_low_eur": round(vol * uc["low"] * fe), "cost_mid_eur": round(vol * uc["mid"] * fe), "cost_high_eur": round(vol * uc["high"] * fe),
            "cost_wall_eur": round(length_km * 1000 * PARAMS["wall"]["unit_cost_eur_m"] * fw),
            "cost_benchmark_uah": round(length_km * PARAMS["benchmark_ua"]["cost_uah_per_km"])}


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
    dcols = [f"damage_L{k}" for k in keys]
    adm3 = gpd.read_parquet(ADM3)
    reg = adm3[adm3["pcode"].str.startswith(region_code)].to_crs(3035)
    fac = pd.read_csv(HERE / "price_factors.csv")
    factor = {(r.variant, r["class"]): r.factor for _, r in fac.iterrows()}
    aep_c, aep_s = PARAMS["ead"]["aep_max_central"], PARAMS["ead"]["aep_max_sensitivity"]
    # ---- gauges with a flood study
    sc = pd.read_parquet(SCEN)
    sc = sc[(sc["status"] == "ok") & (sc["station_type"] == "river") & sc[["high_depth_m_gumbel", "medium_depth_m_gumbel"]].notna().all(axis=1)]
    g = gpd.GeoDataFrame(sc, geometry=gpd.points_from_xy(sc["lon"], sc["lat"]), crs=4326).to_crs(3035)
    # ---- damaged buildings per hromada (centroids of the exposure); HAND of a building = 5 − its depth at 5 m
    expo = gpd.read_file(out / "exposure" / "buildings.gpkg")[["object_id", "geometry"]]
    expo["geometry"] = expo.geometry.centroid
    b = dmg.merge(expo, on="object_id").pipe(gpd.GeoDataFrame, geometry="geometry", crs=3035)
    hit = b[b[dcols].gt(0).any(axis=1)].copy()
    top = f"depth_L{keys[-1]}"
    hit["hand_m"] = (float(keys[-1]) - hit[top]).where(hit[top].notna()) if top in hit else np.nan
    # ---- gauge per hromada (a return-period hazard carries its own probabilities: no gauge, no dike)
    links = []
    for _, h in reg.iterrows():
        pts = hit[hit["pcode"] == h["pcode"]]
        if pts.empty:
            continue
        if by_aep:
            links.append({"pcode": h["pcode"], "name": h["name_ua"], "post_id": None, "gauge_name": summ.get("hazard_label"), "how": "hazard_aep"}); continue
        c = pts.geometry.union_all().centroid
        inside = g[g.within(h.geometry)]
        if not inside.empty:
            gg = inside.loc[inside["F_km2"].fillna(0).idxmax()]; how = "inside"; dist = gg.geometry.distance(c) / 1000
        else:
            d = g.geometry.distance(c)
            if d.min() / 1000 > MAX_GAUGE_KM:
                links.append({"pcode": h["pcode"], "name": h["name_ua"], "post_id": None, "how": "none"}); continue
            gg = g.loc[d.idxmin()]; how = "nearest"; dist = d.min() / 1000
        rises = gumbel_rises(float(gg["high_depth_m"]), float(gg["medium_depth_m"]), float(gg["low_depth_m"]),
                             float(gg["high_depth_m_gumbel"]), float(gg["medium_depth_m_gumbel"]))
        onset = (float(gg["flood_plain_cm"]) - float(gg["low_water_cm"])) / 100 if pd.notna(gg["flood_plain_cm"]) and pd.notna(gg["low_water_cm"]) else None
        dist_h = district_of(pts.geometry)
        gauge_dist = "5.3" if str(gg["basin_code"]).startswith("5.3") else "М5" if str(gg["basin_code"]).startswith("М5") else str(gg["basin_code"])[:3]
        links.append({"pcode": h["pcode"], "name": h["name_ua"], "post_id": int(gg["post_id"]), "gauge_name": gg["post_name_ua"],
                      "river": gg["river"], "F_km2": gg["F_km2"], "how": how, "dist_km": round(float(dist), 1),
                      "same_basin": bool(dist_h == gauge_dist), "hromada_district": dist_h, "gauge_district": gauge_dist,
                      "rise_10pct_m": round(float(gg["high_depth_m"]), 2), "rise_1pct_m": round(float(gg["medium_depth_m"]), 2),
                      "rise_02pct_m": round(float(gg["low_depth_m"]), 2), "rises_T": rises, "onset_m": None if onset is None else round(max(onset, 0.0), 2),
                      "record_m": gg["record_depth_m"], "n_years": int(gg["n_years"]), "distribution": gg["distribution"]})
    L = pd.DataFrame(links)
    if not by_aep:
        log(f"gauges: inside {int((L.how == 'inside').sum())}, nearest {int((L.how == 'nearest').sum())} (other basin "
            f"{int(((L.how == 'nearest') & ~L.same_basin.fillna(False)).sum())}), none {int((L.how == 'none').sum())}; onset known for {int(L.onset_m.notna().sum())}")
    # ---- floor raising: FIAT re-runs
    floor_dmg: dict[float, dict] = {}
    if not a.skip_floor:
        base = pd.read_csv(out / "exposure" / "buildings.csv")
        cls = dmg.set_index("object_id")["cls"]
        for hgt in FLOOR_RAISE_M:
            e = base.copy()
            e["ground_flht"] = np.where(e["object_id"].map(cls) == "RES", GROUND_FLOOR_M + hgt, GROUND_FLOOR_M)
            name = f"buildings_floor{int(hgt * 100):03d}.csv"
            e.to_csv(out / "exposure" / name, index=False)
            floor_dmg[hgt] = {}
            for lv in keys:
                t = time.time()
                floor_dmg[hgt][lv] = fiat_run(out, f"floor{int(hgt * 100):03d}", lv, name)
                log(f"floor +{hgt} m, L={lv}: {time.time() - t:.0f} s, damage {floor_dmg[hgt][lv].sum() / 1e6:,.1f} M€ (was {dmg[f'damage_L{lv}'].sum() / 1e6:,.1f})")
    # ---- river lines for the dike length
    riv = gpd.read_parquet(RIVERS).to_crs(3035)
    riv = riv[riv.intersects(reg.union_all())]
    # ---- EAD and measures per hromada
    rows, meas = [], []
    for _, lk in L.iterrows():
        if lk["how"] == "none":
            continue
        pc = lk["pcode"]
        onset = None if by_aep else lk.get("onset_m")
        onset = None if onset is None or (isinstance(onset, float) and np.isnan(onset)) else float(onset)
        if by_aep:
            fc = fs = lambda k: aep_c if k <= 0 else 1.0 / k  # noqa: E731
        else:
            if not isinstance(lk["rises_T"], dict):
                continue
            fc, fs = aep_of_level(lk["rises_T"], aep_c), aep_of_level(lk["rises_T"], aep_s)
            if fc is None:
                continue
        hb = b[b["pcode"] == pc]
        D = [float(hb[c].sum()) for c in dcols]
        by_cls = {cl: [float(hb.loc[hb.cls == cl, c].sum()) for c in dcols] for cl in ("RES", "COM", "IND")}

        def priced(ds_by_cls, variant, f=fc, on=onset):
            return sum(ead(levels, ds, f, on) * factor[(variant, cl)] for cl, ds in ds_by_cls.items())
        e0 = ead(levels, D, fc, onset)
        e_low = ead(levels, D, fs, onset)
        e_high = ead(levels, D, fc, None)
        row = {"run": a.run, "pcode": pc, "name": lk["name"], "post_id": lk["post_id"], "gauge_name": lk["gauge_name"], "gauge_how": lk["how"],
               "gauge_dist_km": lk.get("dist_km"), "same_basin": lk.get("same_basin"), "onset_m": onset,
               "rise_10pct_m": lk.get("rise_10pct_m"), "rise_1pct_m": lk.get("rise_1pct_m"), "rise_02pct_m": lk.get("rise_02pct_m"),
               "rises_T": json.dumps({str(T): round(v, 2) for T, v in lk["rises_T"].items()}) if isinstance(lk.get("rises_T"), dict) else None,
               "aep_by_level": json.dumps({str(k): round(fc(float(k)), 4) for k in keys}),
               "ead_eur2010": round(e0), "ead_low_eur2010": round(e_low), "ead_high_eur2010": round(e_high),
               "ead_eur_eurostat": round(priced(by_cls, "eur_eurostat")), "ead_uah_sssu": round(priced(by_cls, "uah_sssu"))}
        rows.append(row)
        # dike designs
        hit_h = hit[hit["pcode"] == pc]
        bank = riv[riv.intersects(reg.loc[reg.pcode == pc, "geometry"].iloc[0])]
        near = bank.intersection(hit_h.geometry.buffer(DIKE_REACH_M).union_all()) if len(hit_h) else gpd.GeoSeries([], crs=3035)
        length_km = float(shapely.length(near.union_all()) / 1000) if len(near) else 0.0
        bank_m = float(np.nanpercentile(hit_h["hand_m"], 25)) if hit_h["hand_m"].notna().any() else 0.0
        for label, T in (() if by_aep else DESIGNS):
            H = lk["rises_T"][T] + FREEBOARD_M
            kept = {cl: [d if lv > H else 0.0 for lv, d in zip(levels, ds)] for cl, ds in by_cls.items()}
            Dk = [sum(kept[cl][i] for cl in kept) for i in range(len(levels))]
            e1 = ead(levels, Dk, fc, onset)
            protected = int((hb[[c for c, lv in zip(dcols, levels) if lv <= H]].gt(0).any(axis=1)).sum()) if any(lv <= H for lv in levels) else 0
            h_struct = max(H - bank_m, PARAMS["embankment"]["min_structural_height_m"])
            meas.append({"run": a.run, "pcode": pc, "measure": "dike", "design": label, "design_aep": 1 / T, "height_m": round(H, 2),
                         "bank_m": round(bank_m, 2), "height_struct_m": round(h_struct, 2),
                         "protects_levels_le_m": max([lv for lv in levels if lv <= H], default=0), "buildings_protected": protected,
                         "length_km": round(length_km, 2), **embankment_cost(length_km, h_struct),
                         "avoided_ead_eur2010": round(e0 - e1), "residual_ead_eur2010": round(e1),
                         "avoided_ead_eur_eurostat": round(priced(by_cls, "eur_eurostat") - priced(kept, "eur_eurostat")),
                         "avoided_ead_uah_sssu": round(priced(by_cls, "uah_sssu") - priced(kept, "uah_sssu"))})
        for hgt, per_lv in floor_dmg.items():
            ids = hb["object_id"]
            raised = {cl: [float(per_lv[lv].reindex(ids[hb.cls == cl]).fillna(0).sum()) for lv in keys] for cl in ("RES", "COM", "IND")}
            Dr = [sum(raised[cl][i] for cl in raised) for i in range(len(levels))]
            e1 = ead(levels, Dr, fc, onset)
            meas.append({"run": a.run, "pcode": pc, "measure": "floor", "design": f"+{hgt:g} m", "design_aep": None, "height_m": hgt,
                         "bank_m": None, "height_struct_m": None, "protects_levels_le_m": None,
                         "buildings_protected": int((hb.loc[hb.cls == "RES", dcols].gt(0).any(axis=1)).sum()),
                         "length_km": None, "volume_m3": None, "cost_low_eur": None, "cost_mid_eur": None, "cost_high_eur": None, "cost_wall_eur": None, "cost_benchmark_uah": None,
                         "avoided_ead_eur2010": round(e0 - e1), "residual_ead_eur2010": round(e1),
                         "avoided_ead_eur_eurostat": round(priced(by_cls, "eur_eurostat") - priced(raised, "eur_eurostat")),
                         "avoided_ead_uah_sssu": round(priced(by_cls, "uah_sssu") - priced(raised, "uah_sssu"))})
    E = pd.DataFrame(rows).sort_values("ead_eur2010", ascending=False)
    M = pd.DataFrame(meas)
    pg = out / "pg"
    pg.mkdir(exist_ok=True)
    E.to_csv(pg / "hromada_ead.csv", index=False)
    M.to_csv(pg / "measures.csv", index=False)
    L.drop(columns=["rises_T"], errors="ignore").to_csv(pg / "gauge_links.csv", index=False)
    method = (f"Gumbel fit of the gauge's annual stage rise (10 % and 1 % quantiles) → rises of T = {TS} y; ln AEP piecewise linear in "
              f"the rise, capped at {aep_c} (sensitivity {aep_s}); damage onset at the gauge's flood-plain level where known; "
              f"trapezoid EAD; dike = design rise + {FREEBOARD_M} m freeboard, overtopping only; embankment cost by volume "
              f"(crest {PARAMS['embankment']['crest_width_m']} m, slopes 1:{PARAMS['embankment']['slope_h_per_v']:g}) at sensitivity unit costs "
              f"({PARAMS['embankment']['unit_cost_price_year']} prices); preliminary — not validated, hydrodynamic modelling needed" if not by_aep else
              "AEP = 1 / T of the hazard scenarios; trapezoid EAD; no dike design without water-surface elevations")
    _, cost_year = eurostat_factor(PARAMS["embankment"]["unit_cost_price_year"])
    PARAMS["cost_price"]["cost_price_year"] = cost_year
    (pg / "params.json").write_text(json.dumps(PARAMS, ensure_ascii=False, indent=1))
    s = {"run": a.run, "method": method, "params": PARAMS, "ead_eur2010": round(float(E.ead_eur2010.sum())),
         "ead_low_eur2010": round(float(E.ead_low_eur2010.sum())), "ead_high_eur2010": round(float(E.ead_high_eur2010.sum())),
         "ead_eur_eurostat": round(float(E.ead_eur_eurostat.sum())), "ead_uah_sssu": round(float(E.ead_uah_sssu.sum())), "hromadas": len(E),
         "onset_known": int(E.onset_m.notna().sum()) if "onset_m" in E else 0,
         "top": E.head(8)[["pcode", "name", "gauge_name", "ead_eur2010", "ead_low_eur2010", "ead_high_eur2010"]].to_dict("records"),
         "measures_region": M.groupby(["measure", "design"])[["avoided_ead_eur2010", "buildings_protected"]].sum().reset_index().to_dict("records") if len(M) else []}
    (out / "summary_ead.json").write_text(json.dumps(s, ensure_ascii=False, indent=1, default=float))
    log(f"EAD {s['ead_eur2010'] / 1e6:,.1f} M€/yr (2010), range {s['ead_low_eur2010'] / 1e6:,.1f}–{s['ead_high_eur2010'] / 1e6:,.1f}, "
        f"{len(E)} hromadas, onset known for {s['onset_known']}; done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
