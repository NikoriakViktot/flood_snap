"""Subbasins (QSWAT+ method) on a synthetic district: a main valley along row 60 falling west and a tributary valley
along column 80 falling south into it. Rows = y, columns = x, 30 m cells (120×120 = 12.96 km²).

The district's staged d8 / streams are computed from the same DEM by WhiteboxTools in the fixture, as the national
terrain is. With a 1 km² threshold the network has three links (the main stem above and below the confluence and the
tributary); a gauge on the lower main stem splits its link, so four subbasins."""
from __future__ import annotations

import json

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

from lab_service.subbasins import pipeline as SP
from lab_service.subbasins import runner as SR
from lab_service.subbasins import validate as SV
from lab_service import validate as V

CRS = "EPSG:3035"
TR = from_origin(5_000_000, 3_000_000, 30, 30)
N = 120
ROW, TCOL = 60, 80


def _write(p, a, nodata, dtype):
    with rasterio.open(p, "w", driver="GTiff", height=a.shape[0], width=a.shape[1], count=1, dtype=dtype, crs=CRS,
                       transform=TR, nodata=nodata) as d:
        d.write(a.astype(dtype), 1)


def _lonlat(row, col):
    x, y = rasterio.transform.xy(TR, row, col)
    return Transformer.from_crs(CRS, "EPSG:4326", always_xy=True).transform(x, y)


@pytest.fixture()
def wbt():
    whitebox = pytest.importorskip("whitebox")
    w = whitebox.WhiteboxTools()
    w.set_verbose_mode(False)
    return w


@pytest.fixture()
def district(tmp_path, wbt):
    r = np.arange(N)[:, None].astype(float)
    c = np.arange(N)[None, :].astype(float)
    main = 100 + 0.15 * c + 0.5 * np.abs(r - ROW)
    trib = 100 + 0.15 * TCOL + 0.2 * (ROW - r) + 0.5 * np.abs(c - TCOL) + 0.05
    dem = np.where(r < ROW, np.minimum(main, trib), main).astype(np.float32)
    _write(tmp_path / "dem_f32.tif", dem, -9999, "float32")
    wbt.set_working_dir(str(tmp_path))
    assert wbt.breach_depressions(dem=str(tmp_path / "dem_f32.tif"), output=str(tmp_path / "b.tif"), fill_pits=True) == 0
    assert wbt.d8_pointer(dem=str(tmp_path / "b.tif"), output=str(tmp_path / "d8.tif")) == 0
    assert wbt.d8_flow_accumulation(i=str(tmp_path / "b.tif"), output=str(tmp_path / "acc.tif"), out_type="cells") == 0
    assert wbt.extract_streams(flow_accum=str(tmp_path / "acc.tif"), output=str(tmp_path / "streams.tif"), threshold=200.0,
                               zero_background=True) == 0
    return {n: tmp_path / f"{n}.tif" for n in ("d8", "streams", "dem_f32")}


def _rivers():
    def line(cells):
        return {"type": "LineString", "coordinates": [list(_lonlat(rr, cc)) for rr, cc in cells]}
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "Головна"}, "geometry": line([(ROW, 119), (ROW, 0)])},
        {"type": "Feature", "properties": {"name": "Притока"}, "geometry": line([(0, TCOL), (ROW, TCOL)])}]}


def _params(**kw):
    lon, lat = _lonlat(ROW, 3)
    glon, glat = _lonlat(ROW, 40)
    return {"subbasin_set_id": "s1", "basin_id": "b1", "outlet_lon": lon, "outlet_lat": lat, "region": "5.3.2",
            "dem": "fab_dem", "stream_threshold_km2": 1.0, "burn_depth_m": 50.0, "district_slug": "5_3_2",
            "gauges": [{"post_id": 42001, "lon": glon, "lat": glat, "area_km2": None, "name": "Пост"}],
            "rivers": _rivers(), **kw}


@pytest.fixture()
def run(district, wbt, tmp_path):
    stages = []
    r = SP.run_subbasins(wbt, district, _params(), tmp_path / "tmp", progress=stages.append)
    r["_stages"] = stages
    return r


def test_three_links_and_a_gauge_make_four_subbasins(run):
    feats = run["subbasins"]["features"]
    assert run["summary"]["subbasins"] == 4 == len(feats)
    props = {f["properties"]["id"]: f["properties"] for f in feats}
    root = props[1]
    assert root["down"] is None and root["depth"] == 0                     # ids grow upstream from the outlet
    gauge = [p for p in props.values() if p["post_id"] == 42001]
    assert len(gauge) == 1 and gauge[0]["down"] == 1                         # the gauge splits the lower main stem
    names = sorted(p["name"] for p in props.values() if p["name"])
    assert "Притока" in names and "Головна" in names                         # names from the river layer


def test_areas_close_and_the_network_has_one_outlet(run):
    a = run["audit"]
    assert a["single_outlet"] and a["outlets"] == [1]
    assert a["closure_worst_rel"] < 1e-6                                     # total = local + Σ upstream totals
    total = {f["properties"]["id"]: f["properties"]["total_km2"] for f in run["subbasins"]["features"]}
    assert a["sum_local_km2"] == pytest.approx(total[1], abs=0.01)           # Σ local = the basin
    assert run["summary"]["area_km2"] == pytest.approx(total[1], abs=0.1)


def test_strahler_and_geometries(run):
    feats = run["subbasins"]["features"]
    orders = sorted(f["properties"]["strahler"] for f in feats)
    assert orders[-1] == 2 and orders[0] == 1                               # two first-order links meet: order 2 below
    assert all(f["geometry"] and f["geometry"]["type"] in ("Polygon", "MultiPolygon") for f in feats)
    assert len(run["streams"]["features"]) >= 3
    assert run["_stages"][:3] == ["watershed", "burn", "conditioning"]


def test_gauge_audit_and_products(run):
    g = run["gauges"][0]
    assert g["status"] == "snapped" and g["snap_m"] <= 300
    prods = SR.products_of({"dem": "fab_dem", "stream_threshold_km2": 1.0}, run, "h")
    assert [p["kind"] for p in prods] == ["subbasins", "subbasin_streams", "subbasin_table"]
    import base64
    fc = json.loads(base64.b64decode(prods[0]["content_b64"]))
    assert fc["type"] == "FeatureCollection" and len(fc["features"]) == 4
    csv = base64.b64decode(prods[2]["content_b64"]).decode().splitlines()
    assert csv[0].startswith("id,name,post_id,down") and len(csv) == 5


def test_no_river_layer_warns_and_still_delineates(district, wbt, tmp_path):
    r = SP.run_subbasins(wbt, district, _params(rivers=None), tmp_path / "t2")
    assert r["summary"]["subbasins"] == 4 and any("not burnt" in w for w in r["warnings"])


def test_threshold_too_large_for_the_basin_is_refused(district, wbt, tmp_path):
    from lab_service.pipeline import LabError
    with pytest.raises(LabError, match="too large"):
        SP.run_subbasins(wbt, district, _params(stream_threshold_km2=500.0), tmp_path / "t3")


def test_strahler_helper():
    up = {1: [2, 3], 2: [4, 5], 3: []}
    assert SP.strahler([1, 2, 3, 4, 5], up) == {4: 1, 5: 1, 2: 2, 3: 1, 1: 2}


class _Terrain:
    def available(self, dem): return True
    def has(self, dem, slug, name): return True
    def district_by_code(self, code):
        from types import SimpleNamespace
        return SimpleNamespace(code=code, slug="5_3_2", name_ua="Прут") if code == "5.3.2" else None
    def find_district(self, lon, lat): return self.district_by_code("5.3.2")
    def districts(self): return [self.district_by_code("5.3.2")]


def test_validation_defaults_and_refusals():
    p = SV.prepare_subbasins({"outlet_lon": 26.6, "outlet_lat": 48.26, "region": "5.3.2"}, _Terrain())
    assert p["stream_threshold_km2"] == 25.0 and p["burn_depth_m"] == 50.0 and p["dem"] == "fab_dem" and p["gauges"] == []
    for bad in ({"stream_threshold_km2": 0.5}, {"burn_depth_m": -1}, {"gauges": [{"post_id": "x", "lon": 1, "lat": 2}]},
                {"rivers": {"type": "Feature"}}):
        with pytest.raises(V.Rejected):
            SV.prepare_subbasins({"outlet_lon": 26.6, "outlet_lat": 48.26, "region": "5.3.2", **bad}, _Terrain())


def test_contract_is_valid_json_and_lists_the_kind():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "contracts"
    c = json.loads((root / "subbasin_delineation.v1.json").read_text())
    assert c["$id"] == "geoproc/subbasin_delineation.v1"
    ev = json.loads((root / "job_event.v1.json").read_text())
    assert "subbasin_delineation" in ev["properties"]["payload"]["properties"]["result"]["properties"]["kind"]["enum"]
