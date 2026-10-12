"""Lab pipeline on a synthetic district: a 60x60 grid draining east to west into one outlet column."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from lab_service import pipeline as P

CRS = "EPSG:3035"
TR = from_origin(5_000_000, 3_000_000, 30, 30)       # somewhere in EPSG:3035
H = W = 60


def _write(p: Path, a: np.ndarray, nodata, dtype):
    with rasterio.open(p, "w", driver="GTiff", height=a.shape[0], width=a.shape[1], count=1, dtype=dtype, crs=CRS, transform=TR, nodata=nodata) as d:
        d.write(a.astype(dtype), 1)


@pytest.fixture()
def district(tmp_path):
    # D8 pointer: WBT code 16 = west; every cell flows west; column 0 flows north (code 64) into the corner... keep it simple:
    d8 = np.full((H, W), 32, np.uint8)            # WBT codes: 1 NE, 2 E, 4 SE, 8 S, 16 SW, 32 W, 64 NW, 128 N
    d8[:, 0] = 128                                 # column 0 flows north
    d8[0, 0] = 0
    streams = np.zeros((H, W), np.uint8)
    streams[30, :] = 1                             # one river along row 30
    streams[:, 0] = 1
    dem = (np.arange(W)[None, :] * 2 + 100).repeat(H, 0).astype(np.int16)   # rises eastwards
    hand = (np.abs(np.arange(H)[:, None] - 30) * 0.5).repeat(W, 1).astype(np.float64)
    hand[streams == 1] = 0
    _write(tmp_path / "d8.tif", d8, 0, "uint8")
    _write(tmp_path / "streams.tif", streams, 0, "uint8")
    _write(tmp_path / "dem.tif", dem, -32768, "int16")
    _write(tmp_path / "hand.tif", hand, -9999, "float64")
    return {n: tmp_path / f"{n}.tif" for n in ("d8", "streams", "hand", "dem")}


def _lonlat(row, col):
    from pyproj import Transformer
    x, y = rasterio.transform.xy(TR, row, col)
    return Transformer.from_crs(CRS, "EPSG:4326", always_xy=True).transform(x, y)


def test_snap_picks_the_nearest_stream_cell(district):
    lon, lat = _lonlat(27, 40)                                   # 3 rows above the river
    s = P.snap_outlet(district["streams"], lon, lat, 300)
    assert (s["row"], s["col"]) == (30, 40) and 85 < s["snap_distance_m"] < 95
    with pytest.raises(P.LabError):
        P.snap_outlet(district["streams"], *_lonlat(5, 40), 60)   # 25 rows away, limit 60 m


def test_run_one_statistics_and_polygon(district, tmp_path):
    whitebox = pytest.importorskip("whitebox")
    wbt = whitebox.WhiteboxTools()
    wbt.set_verbose_mode(False)
    lon, lat = _lonlat(30, 40)
    r = P.run_one(wbt, "fab_dem", district, lon, lat, tmp_path / "tmp")
    st = r["stats"]
    # every cell flows west along its own row, so the basin of (30, 40) is the river row east of it: columns 40..59
    assert st["cells"] == 20 and st["area_km2"] == pytest.approx(20 * 900 / 1e6)
    assert st["relief_m"] == 2 * 19
    levels = [x["area_km2"] for x in st["flood_levels"]]
    assert levels == sorted(levels) and st["flood_levels"][-1]["level_m"] == 10
    assert r["basin"]["type"] in ("Polygon", "MultiPolygon") and r["snapped_outlet"]["coordinates"][0] == pytest.approx(lon, abs=1e-4)
    assert st["quality"]["edge_touch"] is True                  # the synthetic basin reaches the raster edge
    c = P.compare(r, r)
    assert c["iou"] == pytest.approx(1.0) and c["area_ratio_fab_to_cop"] == 1.0


def test_basin_too_large(district, tmp_path, monkeypatch):
    whitebox = pytest.importorskip("whitebox")
    from lab_service import settings
    monkeypatch.setattr(settings, "MAX_BASIN_KM2", 0.01)
    wbt = whitebox.WhiteboxTools()
    wbt.set_verbose_mode(False)
    with pytest.raises(P.LabError, match="too large"):
        P.run_one(wbt, "fab_dem", district, *_lonlat(30, 40), tmp_path / "tmp")
