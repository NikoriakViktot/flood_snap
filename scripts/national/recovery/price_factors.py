"""
Price levels for the Recovery Lab damage (JRC max damage is in € of 2010): two variants, two map layers (owner, 2026-10-19).

  eur_eurostat  € of the latest EU-27 year: Eurostat construction cost index, new residential buildings (sts_copi_a,
                EU27_2020, I21, NSA). The EU-27 aggregate ends in 2023 → € 2023.
  uah_sssu      UAH of the current year: € 2010 × the NBU EUR rate of 2010 (annual mean) × the SSSU construction price
                index (DF_PRICE_CHANGE_CONSTRUCTION, «to the corresponding period of the previous year», December of every
                year chained 2011 → last year, then the current year to date): residential dwellings (01011) for RES,
                non-residential dwellings (01012) for COM / IND.

Writes recovery/price_index.csv (the series, long) and recovery/price_factors.csv (variant, class, factor, currency, year).
  ~/repo/data_dnipro_h_q/.venv-linux/bin/python scripts/national/recovery/price_factors.py
"""
from __future__ import annotations

import io
import json
import urllib.request
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
UA = {"User-Agent": "Mozilla/5.0 (GeoHydroAI recovery price factors)"}
SSSU = ("https://stat.gov.ua/sdmx/workspaces/default:integration/registry/sdmx/3.0/data/dataflow/SSSU/"
        "DF_PRICE_CHANGE_CONSTRUCTION/15.1.0/*")
EUROSTAT = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/sts_copi_a?format=JSON&lang=EN"
            "&geo=EU27_2020&indic_bt=COST&unit=I21&s_adj=NSA&sinceTimePeriod=2010")
NBU = "https://bank.gov.ua/NBU_Exchange/exchange_site?start=20100101&end=20101231&valcode=eur&sort=exchangedate&order=asc&json"
CLASS_SSSU = {"RES": "01011", "COM": "01012", "IND": "01012"}


def get(url: str, accept: str | None = None) -> bytes:
    req = urllib.request.Request(url, headers=UA | ({"Accept": accept} if accept else {}))
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def main():
    rows = []
    # NBU: per 100 EUR in the 2010 archive
    fx = pd.DataFrame(json.loads(get(NBU)))
    rate = fx["rate"].astype(float).mean()
    rate = rate / 100 if rate > 100 else rate
    # Eurostat
    e = json.loads(get(EUROSTAT))
    dims, size = e["id"], e["size"]
    idx = {k: e["dimension"][k]["category"]["index"] for k in dims}

    def key(sel):
        n = 0
        for k, s in zip(dims, size):
            n = n * s + idx[k][sel[k]]
        return str(n)
    base = {k: next(iter(idx[k])) for k in dims}
    eu = {int(y): e["value"].get(key({**base, "time": y})) for y in idx["time"]}
    eu = {y: v for y, v in eu.items() if v is not None}
    for y, v in eu.items():
        rows.append({"source": "eurostat", "series": "sts_copi_a EU27_2020 COST residential I21", "period": str(y), "value": v,
                     "base": "2021=100"})
    last_eu = max(eu)
    f_eu = eu[last_eu] / eu[2010]
    # SSSU
    d = pd.read_csv(io.BytesIO(get(SSSU, "application/vnd.sdmx.data+csv;version=2.0.0;labels=both")), low_memory=False)
    d = d[d["REGION: Region"].str.startswith("UA00000000000000000") & d["BASE_PERIOD: Base period"].str.startswith("CORPERIOD_PREV_YEAR")]
    factors = []
    for cls, code in CLASS_SSSU.items():
        s = d[d["CONSTRUCTION_TYPE: Other breakdowns"].str.startswith(code)].set_index("TIME_PERIOD: Time period")["OBS_VALUE: Observation value"].astype(float)
        for p, v in s.items():
            rows.append({"source": "sssu", "series": f"DF_PRICE_CHANGE_CONSTRUCTION {code} to the corresponding period of the previous year",
                         "period": p, "value": v, "base": "same period of the previous year = 100"})
        years = sorted({int(p[:4]) for p in s.index if p.endswith("-M12")})
        f = 1.0
        for y in range(2011, years[-1] + 1):
            f *= s[f"{y}-M12"] / 100
        cur = sorted(p for p in s.index if p.startswith(str(years[-1] + 1)))
        if cur:
            f *= s[cur[-1]] / 100
        through = cur[-1] if cur else f"{years[-1]}-M12"
        factors.append({"variant": "uah_sssu", "class": cls, "factor": round(rate * f, 4), "currency": "UAH",
                        "price_year": int(through[:4]), "note": f"€2010 × NBU {rate:.4f} UAH/€ × SSSU {code} 2010→{through} ×{f:.3f}"})
        factors.append({"variant": "eur_eurostat", "class": cls, "factor": round(f_eu, 4), "currency": "EUR", "price_year": last_eu,
                        "note": f"€2010 × Eurostat EU27 construction cost (residential) {eu[2010]}→{eu[last_eu]} ({last_eu})"})
        factors.append({"variant": "eur_2010_jrc", "class": cls, "factor": 1.0, "currency": "EUR", "price_year": 2010, "note": "JRC as published"})
    pd.DataFrame(rows).drop_duplicates(["source", "series", "period"]).to_csv(HERE / "price_index.csv", index=False)
    pd.DataFrame(factors).drop_duplicates(["variant", "class"]).to_csv(HERE / "price_factors.csv", index=False)
    print(pd.DataFrame(factors).drop_duplicates(["variant", "class"]).to_string(index=False))


if __name__ == "__main__":
    main()
