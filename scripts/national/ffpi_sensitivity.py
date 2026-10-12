"""
Sensitivity of the FFPI to its weighting: v1 (2S+L+H+V)/5 against the variants of ffpi_recombine.py.

Compares, per variant: the national distribution; the area-weighted mean, p90 and share of FFPI >= 7 over five
contrasting regions (hromadas whose centroid is in the region box); the large cities; the small gauge basins
(<= 500 km²); and how much the hromada ranking changes (Spearman rank correlation with v1, top-50 overlap).
It does NOT decide which weighting is right: that needs independent flash-flood events.

  ~/repo/data_dnipro_h_q/.venv-linux/bin/python scripts/national/ffpi_sensitivity.py v2 v2eq v2s3
Writes data/national/ffpi_review_<dem>/sensitivity.{json,md}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
DEM = "fab_dem"
REVIEW = DATA / "national" / f"ffpi_review_{DEM}"
REGIONS = {  # lon_min, lat_min, lon_max, lat_max
    "Carpathians (mountains)": (22.6, 47.75, 25.4, 49.0),
    "Crimean mountains": (33.6, 44.4, 35.3, 44.95),
    "Polissia": (23.8, 51.0, 32.5, 52.4),
    "Steppe (south)": (30.5, 46.3, 37.5, 47.9),
    "Podillia upland": (25.5, 48.4, 29.5, 49.6),
}
CITIES = {"Київ": "Київ", "Харківська": "Харківська", "Одеська": "Одеська", "Дніпровська": "Дніпровська", "Львівська": "Львівська"}


def load(tag: str) -> tuple[gpd.GeoDataFrame, pd.DataFrame, dict]:
    base = DATA / "s3_staging" / "hydro" / "lake" if tag == "v1" else REVIEW / tag
    h = gpd.read_parquet(base / "ffpi" / "v1" / "ffpi_hromadas" / "stats.parquet")
    b = pd.read_parquet(base / "ffpi" / "v1" / "ffpi_basins" / f"dem_source={DEM}" / "stats.parquet")
    m = json.loads((base / "ffpi" / "v1" / "manifest.json").read_text())
    return h, b, m


def wmean(d: pd.DataFrame, col: str) -> float | None:
    w = d["area_km2"] * d["valid_share"]
    ok = d[col].notna() & (w > 0)
    return round(float((d.loc[ok, col] * w[ok]).sum() / w[ok].sum()), 3) if ok.any() else None


def summarise(tag: str, h, b, m, basins_area) -> dict:
    c = h.geometry.representative_point()
    out = {"tag": tag, "national": {k: m["national"][k] for k in ("ffpi_mean", "ffpi_median", "share_high", "share_very_high")}, "regions": {}}
    for name, (x0, y0, x1, y1) in REGIONS.items():
        sel = h[(c.x >= x0) & (c.x <= x1) & (c.y >= y0) & (c.y <= y1)]
        out["regions"][name] = {"n_hromadas": int(len(sel)), "ffpi_mean": wmean(sel, "ffpi_mean"), "ffpi_p90_mean": wmean(sel, "ffpi_p90"),
                                "share_high": wmean(sel, "share_high")}
    cities = h[h["name_ua"].isin(CITIES)]
    out["cities"] = {r.name_ua: {"ffpi_mean": round(float(r.ffpi_mean), 2), "share_high": round(float(r.share_high), 4), "pop_high": int(r.pop_high)}
                     for r in cities.itertuples()}
    bb = b.merge(basins_area, on="post_id")
    small = bb[bb["area_km2_basin"] <= 500]
    out["small_basins"] = {"n": int(len(small)), "ffpi_mean_median": round(float(small["ffpi_mean"].median()), 2),
                           "ffpi_mean_p90": round(float(small["ffpi_mean"].quantile(0.9)), 2),
                           "share_with_any_high_5pct": round(float((small["share_high"] >= 0.05).mean()), 3),
                           "top5": small.nlargest(5, "ffpi_mean")[["name", "ffpi_mean", "share_high"]].round(3).values.tolist()}
    out["top10_hromadas"] = h.nlargest(10, "ffpi_mean")[["name_ua", "adm1_ua", "ffpi_mean", "share_high"]].round(3).values.tolist()
    out["top10_by_pop_high"] = h.nlargest(10, "pop_high")[["name_ua", "adm1_ua", "pop_high"]].values.tolist()
    return out


def main():
    tags = ["v1"] + sys.argv[1:]
    basins = gpd.read_parquet(DATA / "s3_staging" / "hydro" / "lake" / "basin_delineation" / "v1" / "gauge_basins_simplified" / f"dem_source={DEM}" / "basins.parquet")
    basins_area = pd.DataFrame({"post_id": basins["post_id"].astype(int), "area_km2_basin": basins["area_km2"]})
    res, frames = [], {}
    for t in tags:
        h, b, m = load(t)
        frames[t] = h.set_index("pcode")["ffpi_mean"]
        res.append(summarise(t, h, b, m, basins_area))
    v1 = frames["v1"]
    for r in res:
        s = frames[r["tag"]]
        both = pd.concat([v1, s], axis=1, keys=["a", "b"]).dropna()
        r["rank_vs_v1"] = {"spearman": round(float(both["a"].rank().corr(both["b"].rank())), 3),
                           "top50_overlap": int(len(set(v1.nlargest(50).index) & set(s.nlargest(50).index)))}
    REVIEW.mkdir(parents=True, exist_ok=True)
    (REVIEW / "sensitivity.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    lines = ["# FFPI sensitivity to the weighting", "", "| | " + " | ".join(r["tag"] for r in res) + " |", "|---|" + "---|" * len(res)]
    row = lambda label, f: lines.append(f"| {label} | " + " | ".join(str(f(r)) for r in res) + " |")
    row("national mean", lambda r: r["national"]["ffpi_mean"])
    row("national share ≥ 7", lambda r: r["national"]["share_high"])
    for name in REGIONS:
        row(f"{name}: mean", lambda r, n=name: r["regions"][n]["ffpi_mean"])
        row(f"{name}: share ≥ 7", lambda r, n=name: r["regions"][n]["share_high"])
    for c in CITIES:
        row(f"city {c}: mean", lambda r, c=c: r["cities"].get(c, {}).get("ffpi_mean"))
    row("small basins: median of means", lambda r: r["small_basins"]["ffpi_mean_median"])
    row("small basins: share with ≥ 5 % high", lambda r: r["small_basins"]["share_with_any_high_5pct"])
    row("hromada rank vs v1 (Spearman)", lambda r: r["rank_vs_v1"]["spearman"])
    row("top-50 overlap with v1", lambda r: r["rank_vs_v1"]["top50_overlap"])
    (REVIEW / "sensitivity.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    for r in res:
        print("\n==", r["tag"], "top10:", r["top10_hromadas"][:6])
        print("   small-basin top5:", r["small_basins"]["top5"])


if __name__ == "__main__":
    main()
