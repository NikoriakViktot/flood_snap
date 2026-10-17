"""
Bounds of every hromada (COD-AB ADM3, EPSG:4326) for the Hydro road-network route (`/exposure/network?bbox=`): Hydro
picks the hromadas that intersect a basin's bbox here and reads only their slices of the pcode-sorted roads, bridges and
settlements tables (no spatial extension needed on the server).

Output: data/s3_staging/hydro/lake/exposure/v1/hromada_bounds.parquet   pcode, xmin, ymin, xmax, ymax (1 769 rows)

  ~/repo/data_dnipro_h_q/.venv-linux/bin/python scripts/national/hromada_bounds.py
"""
from __future__ import annotations

import geopandas as gpd

from buildings_exposure import ADM3, LAKE


def main() -> None:
    a = gpd.read_parquet(ADM3, columns=["pcode", "geometry"]).to_crs(4326)
    b = a.geometry.bounds.round(5)
    out = b.assign(pcode=a["pcode"].values)[["pcode", "minx", "miny", "maxx", "maxy"]]
    out.columns = ["pcode", "xmin", "ymin", "xmax", "ymax"]
    dst = LAKE / "hromada_bounds.parquet"
    out.sort_values("pcode").to_parquet(dst, index=False)
    print(dst, len(out))


if __name__ == "__main__":
    main()
