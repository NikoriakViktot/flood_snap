"""
Catchment area of the stream each cell drains to (km², float32), per terrain v2 district: the first stream cell (≥ 2000
cells, the HAND streams) down the D8 path of every cell, and that cell's accumulation. With HAND it answers «how high above
WHICH river» — a road 1 m above a 2 km² creek is not on the flood plain of the Prut (evacuation.py, 2026-10-19: a flood
at HAND ≤ L over every creek cut 92 % of the Ivano-Frankivsk settlements off at 5 m).

Inputs: data/national/fab_dem/hand_2000_tb/<d>/{d8,streams}.tif, data/national/stream_area/<d>_acc.tif (WhiteboxTools
d8_flow_accumulation of the same pointer, cells). Output: data/national/stream_area/<d>.tif (EPSG:3035, 30 m).

  ~/repo/data_dnipro_h_q/.venv-linux/bin/python scripts/national/stream_area.py 5_3_2 M5_2
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import rasterio

N = Path(__file__).resolve().parents[2] / "data" / "national"
D8 = {1: (-1, 1), 2: (0, 1), 4: (1, 1), 8: (1, 0), 16: (1, -1), 32: (0, -1), 64: (-1, -1), 128: (-1, 0)}  # WhiteboxTools


def one(d: str):
    t0 = time.time()
    with rasterio.open(N / "fab_dem/hand_2000_tb" / d / "d8.tif") as r:
        d8 = r.read(1); prof = r.profile
    with rasterio.open(N / "fab_dem/hand_2000_tb" / d / "streams.tif") as r:
        st = r.read(1) > 0
    with rasterio.open(N / "stream_area" / f"{d}_acc.tif") as r:
        acc = r.read(1).astype(np.float32).ravel()
    h, w = d8.shape
    idx = np.arange(h * w, dtype=np.int64).reshape(h, w)
    nxt = idx.copy()
    for code, (a, b) in D8.items():
        m = d8 == code
        rr, cc = np.nonzero(m)
        tr, tc = rr + a, cc + b
        ok = (tr >= 0) & (tr < h) & (tc >= 0) & (tc < w)
        nxt[rr[ok], cc[ok]] = tr[ok] * w + tc[ok]
    nxt = nxt.ravel()
    del idx, d8
    stv = st.ravel()
    nxt[stv] = np.flatnonzero(stv)                      # stream cells are fixed points: the jump stops at the first one
    for _ in range(40):
        nn = nxt[nxt]
        if np.array_equal(nn, nxt):
            break
        nxt = nn
    area = np.where(stv[nxt], acc[nxt] * 0.0009, np.nan).astype(np.float32).reshape(h, w)
    prof.update(dtype="float32", nodata=np.nan, compress="deflate", predictor=3, tiled=True, blockxsize=512, blockysize=512)
    with rasterio.open(N / "stream_area" / f"{d}.tif", "w", **prof) as o:
        o.write(area, 1)
    print(f"{d}: {h}×{w}, {np.isfinite(area).mean():.1%} of cells drain to a stream, {time.time() - t0:.0f} s", flush=True)


if __name__ == "__main__":
    for d in sys.argv[1:]:
        one(d)
