"""
FFPI per gauge basin and per hromada (ADM3) from the national 100 m stack (ffpi_stack100.py), plus the admin
boundaries themselves. Host Python (rasterio, numpy, geopandas, pyogrio, pyarrow): the worker image has no geopandas.

Zonal statistics per polygon (EPSG:3035, 100 m cells = 0.01 km², all fractions are exact means of 30 m cells):
  area_km2, valid_share, ffpi_mean, ffpi_median, ffpi_p90 (from the distribution of the 100 m cell means),
  share_high (FFPI >= 7), share_very_high (>= 8.5), area_high_km2, area_very_high_km2,
  pop_total, pop_high = Σ pop · high-fraction, pop_high_share, builtup_share_high = Σ builtup·high / Σ high,
  s_mean, l_mean, h_mean, v_mean.
Hromadas also get top_basins: up to 5 gauge basins (<= 2000 km²) that intersect them, ranked by ffpi_mean.

Outputs (lake layout):
  data/s3_staging/hydro/lake/admin/v1/{adm1,adm2,adm3,adm3_simplified}.parquet
  data/s3_staging/hydro/lake/ffpi/v1/ffpi_basins/dem_source=<dem>/stats.parquet
  data/s3_staging/hydro/lake/ffpi/v1/ffpi_hromadas/stats.parquet       (+ simplified geometry)
  data/s3_staging/hydro/lake/ffpi/v1/manifest.json
  data/national/<dem>/ffpi/ffpi_postgis.gpkg   layers admin_adm3, ffpi_hromadas, ffpi_basins (for ogr2ogr)

  ~/repo/data_dnipro_h_q/.venv-linux/bin/python scripts/national/ffpi_zonal.py --dem fab_dem
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
LAKE = DATA / "s3_staging" / "hydro" / "lake"
ADMIN_GDB = "/vsizip//mnt/d/Аспірантура/GIS_uk/ukr_admin_boundaries.gdb.zip/ukr_admin_boundaries.gdb"
CELL_KM2 = 0.01
BANDS = ("ffpi_mean", "valid", "high7", "high85", "builtup", "pop", "s_mean", "l_mean", "h_mean", "v_mean")


def load_admin() -> dict[int, gpd.GeoDataFrame]:
    out = {}
    for lvl in (1, 2, 3):
        g = gpd.read_file(ADMIN_GDB, layer=f"ukr_admin{lvl}", engine="pyogrio")
        cols = {f"adm{lvl}_pcode": "pcode", f"adm{lvl}_name": "name_en", f"adm{lvl}_name1": "name_ua"}
        for p in range(1, lvl):
            cols |= {f"adm{p}_pcode": f"adm{p}_pcode", f"adm{p}_name": f"adm{p}_en", f"adm{p}_name1": f"adm{p}_ua"}
        g = g.rename(columns=cols)
        keep = list(cols.values()) + ["area_sqkm", "valid_on", "version", "geometry"]
        g = g[[c for c in keep if c in g.columns]].to_crs(4326)
        g["valid_on"] = pd.to_datetime(g["valid_on"]).dt.strftime("%Y-%m-%d")
        out[lvl] = g
    return out


class Stack:
    def __init__(self, d: Path):
        self.src = {b: rasterio.open(d / f"{b}.tif") for b in BANDS}
        self.ref = self.src["ffpi_mean"]
        self.crs = self.ref.crs

    def read(self, band: str, win):
        return self.src[band].read(1, window=win, boundless=True, fill_value=0)

    def zonal(self, geom) -> dict:
        b = geom.bounds
        win = from_bounds(*b, self.ref.transform).round_offsets().round_lengths()
        if win.width <= 0 or win.height <= 0:
            return {}
        tr = self.ref.window_transform(win)
        inside = ~geometry_mask([geom], out_shape=(int(win.height), int(win.width)), transform=tr, invert=False)
        n_cells = int(inside.sum())
        if n_cells == 0:
            return {}
        valid = self.read("valid", win)[inside]
        w = valid.sum()
        out = {"area_km2": round(n_cells * CELL_KM2, 3), "valid_share": round(float(w / n_cells), 4)}
        if w <= 0:
            return out
        fm = self.read("ffpi_mean", win)[inside] / 10.0
        out["ffpi_mean"] = round(float((fm * valid).sum() / w), 3)
        order = np.argsort(fm)
        cum = np.cumsum(valid[order]) / w
        out["ffpi_median"] = round(float(fm[order][np.searchsorted(cum, 0.5)]), 2)
        out["ffpi_p90"] = round(float(fm[order][min(np.searchsorted(cum, 0.9), len(fm) - 1)]), 2)
        h7 = self.read("high7", win)[inside]
        h85 = self.read("high85", win)[inside]
        out["share_high"] = round(float(h7.sum() / w), 4)
        out["share_very_high"] = round(float(h85.sum() / w), 4)
        out["area_high_km2"] = round(float(h7.sum() * CELL_KM2), 3)
        out["area_very_high_km2"] = round(float(h85.sum() * CELL_KM2), 3)
        pop = self.read("pop", win)[inside]
        out["pop_total"] = int(round(float(pop.sum())))
        out["pop_high"] = int(round(float((pop * h7).sum())))
        out["pop_high_share"] = round(out["pop_high"] / out["pop_total"], 4) if out["pop_total"] else None
        bu = self.read("builtup", win)[inside]
        out["builtup_share_high"] = round(float((bu * h7).sum() / h7.sum()), 4) if h7.sum() > 0 else None
        for f in ("s", "l", "h", "v"):
            fm_ = self.read(f"{f}_mean", win)[inside]
            out[f"{f}_mean"] = round(float((fm_ * valid).sum() / w), 3)
        return out


def zonal_frame(stack: Stack, g: gpd.GeoDataFrame, key: str) -> pd.DataFrame:
    rows = []
    gg = g.to_crs(stack.crs)
    for k, geom in zip(gg[key], gg.geometry):
        if geom is None or geom.is_empty:
            continue
        rows.append({key: k, **stack.zonal(geom)})
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", default="fab_dem")
    ap.add_argument("--tag", default="", help="variant of ffpi_recombine.py; writes to data/national/<dem>/ffpi_review/<tag>/ "
                                              "instead of the lake (variants are for review, not publication)")
    a = ap.parse_args()
    cfg = json.loads((ROOT / "scripts" / "national" / "ffpi_config.json").read_text())
    stack_dir = DATA / "national" / a.dem / (f"ffpi_{a.tag}" if a.tag else "ffpi") / "_stack100"
    global LAKE
    if a.tag:
        LAKE = DATA / "national" / f"ffpi_review_{a.dem}" / a.tag
    stack = Stack(stack_dir)
    t0 = datetime.now(timezone.utc)

    admin = load_admin()
    adm_dir = LAKE / "admin" / "v1"
    adm_dir.mkdir(parents=True, exist_ok=True)
    for lvl, g in admin.items():
        g.to_parquet(adm_dir / f"adm{lvl}.parquet")
    adm3 = admin[3]
    adm3_s = adm3.copy()
    adm3_s["geometry"] = adm3_s.geometry.simplify(0.0005, preserve_topology=True)
    adm3_s.to_parquet(adm_dir / "adm3_simplified.parquet")
    print("admin:", {lvl: len(g) for lvl, g in admin.items()}, flush=True)

    basins = gpd.read_parquet((DATA / "s3_staging" / "hydro" / "lake") / "basin_delineation" / "v1" / "gauge_basins_simplified" / f"dem_source={a.dem}" / "basins.parquet")
    basins["post_id"] = basins["post_id"].astype(int)
    bstats = zonal_frame(stack, basins, "post_id")
    bstats.insert(1, "dem_source", a.dem)
    bstats = bstats.merge(basins[["post_id", "name", "river", "basin_code"]], on="post_id", how="left")
    bdir = LAKE / "ffpi" / "v1" / "ffpi_basins" / f"dem_source={a.dem}"
    bdir.mkdir(parents=True, exist_ok=True)
    bstats.to_parquet(bdir / "stats.parquet", index=False)
    print("basins:", len(bstats), "mean of means", round(bstats["ffpi_mean"].mean(), 2), flush=True)

    hstats = zonal_frame(stack, adm3, "pcode")
    small = basins[basins["area_km2"] <= 2000][["post_id", "name", "area_km2", "geometry"]].merge(bstats[["post_id", "ffpi_mean"]], on="post_id")
    joined = gpd.sjoin(adm3[["pcode", "geometry"]], small, how="inner", predicate="intersects")
    top = (joined.sort_values("ffpi_mean", ascending=False).groupby("pcode")
           .apply(lambda d: json.dumps([{"post_id": int(r.post_id), "name": r.name, "area_km2": round(float(r.area_km2), 1),
                                         "ffpi_mean": r.ffpi_mean} for r in d.head(5).itertuples()], ensure_ascii=False)))
    hstats["top_basins"] = hstats["pcode"].map(top).fillna("[]")
    meta = adm3_s[["pcode", "name_en", "name_ua", "adm1_pcode", "adm1_en", "adm1_ua", "adm2_pcode", "adm2_en", "adm2_ua", "geometry"]]
    hrom = gpd.GeoDataFrame(meta.merge(hstats, on="pcode", how="left"), geometry="geometry", crs=4326)
    hdir = LAKE / "ffpi" / "v1" / "ffpi_hromadas"
    hdir.mkdir(parents=True, exist_ok=True)
    hrom.to_parquet(hdir / "stats.parquet", index=False)
    print("hromadas:", len(hrom), "top by mean:", hrom.nlargest(5, "ffpi_mean")[["name_ua", "ffpi_mean"]].values.tolist(), flush=True)

    hist = json.loads((stack_dir / "national_hist.json").read_text())
    h = np.asarray(hist["histogram_x10"], dtype=float)
    x = np.arange(101) / 10
    valid = h[1:].sum()
    cum = np.cumsum(h[1:]) / valid
    manifest = {
        "version": cfg["version"], "dem_source": a.dem, "config": cfg, "created_at": t0.isoformat(timespec="seconds"),
        "national": {"area_km2": round(valid * 0.0009, 1), "ffpi_mean": round(float((x[1:] * h[1:]).sum() / valid), 3),
                     "ffpi_median": round(float(x[1:][np.searchsorted(cum, 0.5)]), 2),
                     "share_high": round(float(h[70:].sum() / valid), 4), "share_very_high": round(float(h[85:].sum() / valid), 4),
                     "histogram": [{"bin": i, "share": round(float(h[i] / valid), 6)} for i in range(10, 101)]},
        "units": {"basins": int(len(bstats)), "hromadas": int(len(hrom))},
        "hsg_gap_share": None,
        "definition": "Flash Flood Potential Index: the static disposition of the terrain to fast surface runoff (slope, land use, soils, "
                      "vegetation), 1..10. It is NOT a flood-depth or inundation map and carries no rainfall.",
    }
    runs = [json.loads(p.read_text()) for p in (DATA / "national" / a.dem / "ffpi").glob("*/run.json")]
    if runs:
        manifest["hsg_gap_share"] = round(sum(r["hsg_gap_px"] for r in runs) / max(sum(r["land_px_buffered"] for r in runs), 1), 4)
    (LAKE / "ffpi" / "v1" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))

    gpkg = DATA / "national" / f"ffpi_postgis_{a.dem}.gpkg"          # the ffpi/ folder belongs to the worker (root)
    if gpkg.exists():
        gpkg.unlink()
    adm3.to_file(gpkg, layer="admin_adm3", driver="GPKG")
    pd.DataFrame(hstats).to_csv(gpkg.with_suffix(".hromadas.csv"), index=False)
    gpd.GeoDataFrame(hstats).to_file(gpkg, layer="ffpi_hromadas", driver="GPKG")
    gpd.GeoDataFrame(bstats).to_file(gpkg, layer="ffpi_basins", driver="GPKG")
    print("manifest:", manifest["national"], "gpkg:", gpkg, flush=True)


if __name__ == "__main__":
    main()
