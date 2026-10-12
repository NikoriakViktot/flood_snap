"""
WFP / HDX admin-2 climate series for Ukraine into the data lake (CHIRPS rainfall dekads since 1981, MODIS NDVI dekads
since 2002, per raion, ADM2 pcode of COD-AB). Used as antecedent rainfall context (dekad, 1 month, 3 months and
their anomaly against the long-term mean); not a substitute for gauge or hourly radar rainfall.

  rainfall: date, adm2_pcode, rfh (10-day mm), rfh_avg, r1h (1-month mm), r1h_avg, r3h (3-month mm), r3h_avg,
            rfq / r1q / r3q (% of the long-term mean), status (final | prelim)
  ndvi:     date, adm2_pcode, vim (NDVI), vim_avg, viq (% of the mean)
Coverage: 27 of the 139 raions are missing in the source (e.g. Volyn, Rivne, Sumy, Chernihiv, Kyiv oblasts).

Outputs data/s3_staging/hydro/lake/climate/wfp_adm2/v1/{rainfall,ndvi}.parquet + manifest.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

SRC = Path("/mnt/d/Аспірантура/GIS_uk")
OUT = Path(__file__).resolve().parents[2] / "data/s3_staging/hydro/lake/climate/wfp_adm2/v1"


def load(name: str, cols: dict[str, str]) -> pd.DataFrame:
    d = pd.read_csv(SRC / name, skiprows=[1], dtype={"ADM2_PCODE": "string"})
    d = d.rename(columns={"ADM2_PCODE": "adm2_pcode", **cols})
    d["date"] = pd.to_datetime(d["date"]).dt.date
    keep = ["date", "adm2_pcode", *cols.values()] + (["status"] if "version" in d.columns else [])
    if "version" in d.columns:
        d = d.rename(columns={"version": "status"})
    return d[keep].sort_values(["adm2_pcode", "date"]).reset_index(drop=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rain = load("ukr-rainfall-adm2-full.csv", {c: c for c in ("rfh", "rfh_avg", "r1h", "r1h_avg", "r3h", "r3h_avg", "rfq", "r1q", "r3q")})
    ndvi = load("ukr-ndvi-adm2-full.csv", {c: c for c in ("vim", "vim_avg", "viq")})
    rain.to_parquet(OUT / "rainfall.parquet", index=False)
    ndvi.to_parquet(OUT / "ndvi.parquet", index=False)
    m = {"product": "climate/wfp_adm2", "version": "v1", "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "source": "WFP / HDX admin-2 series (CHIRPS rainfall, MODIS NDVI), downloaded 2025-01", "licence": "CC BY-IGO (HDX)",
         "rainfall": {"rows": len(rain), "raions": int(rain["adm2_pcode"].nunique()), "from": str(rain["date"].min()), "to": str(rain["date"].max())},
         "ndvi": {"rows": len(ndvi), "raions": int(ndvi["adm2_pcode"].nunique()), "from": str(ndvi["date"].min()), "to": str(ndvi["date"].max())},
         "coverage_note": "27 of 139 raions are missing in the source"}
    (OUT / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=1))
    print(json.dumps(m, ensure_ascii=False))


if __name__ == "__main__":
    main()
