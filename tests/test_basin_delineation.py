"""
Real WhiteboxTools run of the basin pipeline on a synthetic DEM (no mocks).

Run inside the dem_worker image:
    docker compose run --rm -v ./tests:/tests -w /app dem_worker python3 -m pytest -q /tests/test_basin_delineation.py

Synthetic terrain (EPSG:32635, 30 m cells): two V-shaped valleys separated by a ridge, both
draining south. The outlet is placed on the west river, so the watershed must be exactly the
west half — the east valley (larger channel nearby) must NOT be captured.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
whitebox = pytest.importorskip("whitebox")
pytest.importorskip("terracotta")

sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dem_worker"))

import terrain as terrain_mod  # noqa: E402
from basin_delineation import DelineationError, delineate  # noqa: E402

EPSG = "EPSG:32635"
RES = 30.0
NX, NY = 120, 160            # 3.6 km × 4.8 km
X0, Y0 = 400_000.0, 5_330_000.0  # UTM 35N, ~ 23.7E 48.1N


def _synthetic_dem() -> np.ndarray:
    cols = np.arange(NX)
    rows = np.arange(NY)
    C, R = np.meshgrid(cols, rows)
    west_river, east_river, ridge = 30, 90, 60
    dist = np.where(C < ridge, np.abs(C - west_river), np.abs(C - east_river))
    z = 500 + 2.0 * dist * RES / 30 * 3   # valley side slope
    z += (NY - R) * 0.5                     # everything drains south (row NY-1)
    z[:, ridge] += 40                        # sharp divide
    return z.astype(np.float32)


@pytest.fixture()
def synthetic_terrain(tmp_path, monkeypatch):
    from rasterio.transform import from_origin
    from whitebox import WhiteboxTools

    monkeypatch.setattr(terrain_mod, "TERRAIN_DIR", tmp_path / "terrain")

    def fake_acquire(dem_source, bbox, epsg, res, out, tmp):
        with rasterio.open(out, "w", driver="GTiff", width=NX, height=NY, count=1, dtype="float32",
                           crs=EPSG, transform=from_origin(X0, Y0, RES, RES), nodata=-9999) as dst:
            dst.write(_synthetic_dem(), 1)

    monkeypatch.setattr(terrain_mod, "_acquire_dem", fake_acquire)
    monkeypatch.setattr(terrain_mod, "STREAM_THRESHOLDS_PRESET", (50, 100, 200))
    wbt = WhiteboxTools()
    wbt.set_verbose_mode(False)
    t = terrain_mod.ensure_terrain(wbt, "copernicus_dem", [23.6, 48.0, 23.8, 48.2], EPSG, RES)
    return wbt, t


def _lonlat(col: float, row: float):
    from pyproj import Transformer
    x = X0 + (col + 0.5) * RES
    y = Y0 - (row + 0.5) * RES
    return Transformer.from_crs(EPSG, "EPSG:4326", always_xy=True).transform(x, y)


def _params(lon, lat, **kw):
    p = {"basin_id": "btest", "run_id": "r1", "project_id": "p1", "outlet_lon": lon, "outlet_lat": lat,
         "snap_distance_m": 150, "stream_threshold": 100, "hand_threshold_m": 3, "max_slope_deg": None,
         "flood_levels_m": [1, 2, 3, 5], "cn_grid_tif": None}
    p.update(kw)
    return p


def test_west_basin_passport(synthetic_terrain, tmp_path):
    wbt, t = synthetic_terrain
    lon, lat = _lonlat(30 + 2, NY - 6)   # 2 cells east of the west river, near the outlet edge
    m = delineate(wbt, t, _params(lon, lat), tmp_path / "products" / "btest")

    s = m["stats"]
    # West half: columns 0..59 drain to the west river → area ≈ 60 cols × ~154 rows × 900 m²
    expected_km2 = 60 * (NY - 6) * RES * RES / 1e6
    assert s["area_km2"] == pytest.approx(expected_km2, rel=0.15)
    assert s["area_km2"] < 0.75 * NX * NY * RES * RES / 1e6, "east valley must not be included"

    # snapping: requested point is 60 m off-channel, snapped to the west river
    assert 0 < m["outlet"]["actual_snap_distance_m"] <= 150
    assert m["outlet"]["requested"] == [lon, lat]
    assert m["outlet"]["snapped"] != m["outlet"]["requested"]

    # pixel area from the transform, not a hard-coded 900
    assert s["pixel_area_m2"] == pytest.approx(RES * RES)

    # HAND inundation: monotonic, connected, labelled as scenarios
    fs = m["flood_scenarios"]
    assert fs["method"] == "hand_inundation_scenario"
    areas = [lv["area_km2"] for lv in fs["levels"]]
    assert areas == sorted(areas) and areas[0] > 0
    assert m["quality"]["flood_area_monotonic"]

    # reproducibility metadata
    for k in ("vertical_datum", "processing_crs", "grid_resolution_m", "version"):
        assert m["dem"][k]
    assert m["software"]["whitebox"]
    assert m["parameters"]["stream_threshold"] == 100

    # files: COGs in 3857 for tiles, GeoJSON in 4326, checksums
    out = tmp_path / "products" / "btest"
    with rasterio.open(out / "tiles" / "hand_100.tif") as src:
        assert src.crs.to_epsg() == 3857
    gj = json.loads((out / "vectors" / "basin.geojson").read_text())
    assert gj["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert -180 <= gj["geometry"]["coordinates"][0][0][0] <= 180 if gj["geometry"]["type"] == "Polygon" else True
    assert "manifest.json" not in m["checksums"]
    assert all(len(v) == 64 for v in m["checksums"].values())

    # tile keys embed the basin id, never contain digits that the UI's replaceAll("2000") could hit
    for p in m["products"]:
        assert "btest" in p["tc_layer"]


def test_outlet_far_from_stream_is_rejected(synthetic_terrain, tmp_path):
    wbt, t = synthetic_terrain
    lon, lat = _lonlat(45, 40)   # mid-slope, ~450 m from either river
    with pytest.raises(DelineationError):
        delineate(wbt, t, _params(lon, lat, snap_distance_m=100), tmp_path / "products" / "bfar")


def test_slope_limit_keeps_channel_and_drops_steep_banks(synthetic_terrain, tmp_path):
    wbt, t = synthetic_terrain
    lon, lat = _lonlat(30 + 2, NY - 6)
    levels = [1, 2, 10, 20]   # banks rise ~6 m per cell: only 10 / 20 m reach them
    free = delineate(wbt, t, _params(lon, lat, max_slope_deg=None, flood_levels_m=levels), tmp_path / "products" / "bfree")
    lim = delineate(wbt, t, _params(lon, lat, max_slope_deg=8.0, flood_levels_m=levels), tmp_path / "products" / "blim")

    a_free = {lv["level_m"]: lv["area_km2"] for lv in free["flood_scenarios"]["levels"]}
    a_lim = {lv["level_m"]: lv["area_km2"] for lv in lim["flood_scenarios"]["levels"]}
    # channel-only levels are unaffected; synthetic banks are ~11° → the limit removes them
    assert a_lim[1] == a_free[1] and a_lim[2] == a_free[2]
    assert a_lim[10] < a_free[10] and a_lim[20] < a_free[20]
    assert all(a > 0 for a in a_lim.values())
    assert any(lv["slope_cells_removed"] > 0 for lv in lim["flood_scenarios"]["levels"])
    assert lim["flood_scenarios"]["max_slope_deg"] == 8.0
    assert lim["parameters"]["max_slope_deg"] == 8.0
    assert lim["quality"]["flood_area_monotonic"]
