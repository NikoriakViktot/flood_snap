"""HAND model on a synthetic district: a 120×120 Float32 valley whose HAND is known analytically.

Rows = y, columns = x. The channel runs along row 60 and falls westwards 0.15 m per cell; the flanks rise 0.5 m per
row away from the channel (cross slope 1.67 %, along-channel 0.5 %: steepest descent is straight N/S, so the D8 path
of (r, c) reaches the channel at (60, c) and HAND = 0.5·|r − 60| exactly). The staged district D8 / streams rasters
encode the same drainage, so the watershed of an outlet at (60, 20) is every column ≥ 20."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from lab_service import settings
from lab_service.hand import pipeline as HP

CRS = "EPSG:3035"
TR = from_origin(5_000_000, 3_000_000, 30, 30)
N = 120
ROW = 60


def _write(p: Path, a: np.ndarray, nodata, dtype):
    with rasterio.open(p, "w", driver="GTiff", height=a.shape[0], width=a.shape[1], count=1, dtype=dtype, crs=CRS, transform=TR, nodata=nodata) as d:
        d.write(a.astype(dtype), 1)


@pytest.fixture()
def district(tmp_path):
    d8 = np.full((N, N), 8, np.uint8)             # WBT codes: 1 NE, 2 E, 4 SE, 8 S, 16 SW, 32 W, 64 NW, 128 N
    d8[ROW + 1:, :] = 128
    d8[ROW, :] = 32
    d8[ROW, 0] = 0
    streams = np.zeros((N, N), np.uint8)
    streams[ROW, :] = 1
    r = np.arange(N)[:, None]
    c = np.arange(N)[None, :]
    dem = (100.0 + 0.15 * c + 0.5 * np.abs(r - ROW)).astype(np.float32)
    _write(tmp_path / "d8.tif", d8, 0, "uint8")
    _write(tmp_path / "streams.tif", streams, 0, "uint8")
    _write(tmp_path / "dem_f32.tif", dem, -9999, "float32")
    return {n: tmp_path / f"{n}.tif" for n in ("d8", "streams", "dem_f32")}


def _lonlat(row, col):
    from pyproj import Transformer
    x, y = rasterio.transform.xy(TR, row, col)
    return Transformer.from_crs(CRS, "EPSG:4326", always_xy=True).transform(x, y)


def _params(threshold_km2=0.5, **kw):
    lon, lat = _lonlat(ROW, 20)
    from lab_service.hand.validate import threshold_cells
    return {"hand_model_id": "m1", "basin_id": "b1", "outlet_lon": lon, "outlet_lat": lat, "region": "5.3.2", "dem": "fab_dem",
            "stream_threshold_km2": threshold_km2, "stream_threshold_cells": threshold_cells(threshold_km2),
            "district_slug": "5_3_2", "district_name_ua": "Прут", **kw}


@pytest.fixture()
def wbt():
    whitebox = pytest.importorskip("whitebox")
    w = whitebox.WhiteboxTools()
    w.set_verbose_mode(False)
    return w


@pytest.fixture()
def run(district, wbt, tmp_path):
    stages = []
    r = HP.run_hand_model(wbt, "fab_dem", district, _params(), tmp_path / "tmp", progress=stages.append)
    r["_stages"] = stages
    return r


def test_hand_matches_the_analytic_valley(run):
    basin, hand = run["basin"], run["hand"]
    assert run["shape"] == [N, N - 20] and basin.sum() == N * (N - 20)      # every column ≥ 20, all rows
    rr = np.arange(N)[:, None]
    cc = np.arange(20, N)[None, :]
    # stream cells need ≥ 556 accumulated cells: 120·(120 − c) ≥ 556 → c ≤ 115; the four easternmost channel cells are
    # not streams, so cells there drain west along the channel to column 115 (+0.15 m per column of HAND)
    expect = 0.5 * np.abs(rr - ROW) + 0.15 * np.maximum(0, cc - 115)
    inside = basin & (hand != HP.NODATA)
    assert inside.sum() > 0.99 * basin.sum()
    assert np.abs(hand[inside] - expect[inside]).max() < 0.02
    assert hand[ROW, :96].max() < 0.02                                 # the channel itself (columns 20..115)


def test_slope_streams_links_and_catchments(run):
    st = run["stats"]
    assert st["streams"]["links"] == 1 and run["reaches"]["reaches"][0]["id"] == 1
    assert 60 <= st["streams"]["cells"] <= 100                           # channel cells with accumulation ≥ 556 cells
    flank = run["slope"][(run["slope"] != HP.NODATA)]
    assert abs(float(np.median(flank)) - 1.0) < 0.1                     # atan(sqrt(0.01667² + 0.005²)) ≈ 1.0°
    # the catchment of the single link is the whole basin, numbered with the link id
    assert (run["catchments"][run["basin"]] == 1).all()
    reach = run["reaches"]["reaches"][0]
    assert abs(reach["slope"] - 0.005) < 0.0002 and reach["length_m"] == pytest.approx(96 * 30) and reach["length_link_m"] > reach["length_m"]
    assert reach["drainage_area_km2"] > 5
    assert abs(reach["slope_deg"] - 0.2865) < 0.03
    assert st["conditioning"]["cells_changed"] < 50                     # a monotone valley needs no breaching
    assert st["quality"]["edge_touch"] is True and st["hand"]["max"] == pytest.approx(30.6, abs=0.05)
    levels = [x["area_km2"] for x in st["flood_levels"]]
    # HAND ≤ 1 m: rows 58..62 (0, 0.5, 1.0 m) × 100 columns, minus the few tail cells east of the last stream cell
    assert levels == sorted(levels) and st["flood_levels"][0]["area_km2"] == pytest.approx(5 * 100 * 900 / 1e6, abs=0.02)
    assert run["_stages"] == ["watershed", "conditioning", "flow", "hand", "slope", "statistics", "rating"]
    assert set(run["timings_s"]) == {"watershed", "terrain", "conditioning", "flow", "hand", "slope", "statistics", "rating"}


def test_window_keeps_a_buffer_before_d8(run):
    w = run["window"]
    assert w["buffer_cells"] == 34 and w["clamped"] is True              # the basin touches the raster edge
    assert w["cols"] == N and w["rows"] == N                              # the buffer west of column 20 stays inside the window


def test_basin_too_large_is_refused(district, wbt, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "HAND_MAX_BASIN_KM2", 1.0)
    from lab_service.pipeline import LabError
    with pytest.raises(LabError, match="exceeds the HAND model limit"):
        HP.run_hand_model(wbt, "fab_dem", district, _params(), tmp_path / "tmp")


def test_align_catchments_remaps_to_link_ids():
    links = np.array([[0, 0, 0], [7, 7, 0], [0, 0, 9]], np.int32)
    streams = links > 0
    catch = np.array([[2, 2, 3], [2, 2, 3], [3, 3, 3]], np.int32)
    out, ok = HP.align_catchments(catch, links, streams)
    assert ok and out.tolist() == [[7, 7, 9], [7, 7, 9], [9, 9, 9]]
    bad = np.array([[2, 2, 2], [2, 2, 2], [2, 2, 2]], np.int32)         # one catchment, two links: no mapping
    out, ok = HP.align_catchments(bad, links, streams)
    assert not ok and (out == bad).all()


def test_package_and_products(run, tmp_path):
    from lab_service.hand import packaging as PK
    from lab_service.hand import products as PR
    from lab_service.hand import provenance as PV
    out = tmp_path / "out"
    files = PK.package(run, out)
    assert set(files) == {"hand", "slope", "dem_conditioning", "stream_links", "catchments", "hand_png", "hand_aux_png"}
    assert run["stats"]["delivery"] == "png"
    with rasterio.open(files["hand"]["path"]) as r:
        assert r.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") == "COG" and r.dtypes == ("float32",) and r.nodata == -9999
        a = r.read(1, masked=True)
        assert a.count() == run["basin"].sum() and float(a.max()) == pytest.approx(30.6, abs=0.05)
    rgba = PK.read_png_rgba(files["hand_png"]["path"].read_bytes())
    hand_m, channel, valid = PK.decode_hand_png(rgba)
    g = files["hand_png"]["stats"]["grid"]
    assert rgba.shape == (g["height"], g["width"], 4) and valid.any() and (~valid).any()
    assert float(hand_m[valid].max()) == pytest.approx(30.6, abs=0.1) and channel[valid].sum() > 30
    assert set(np.unique(rgba[..., 3]).tolist()) <= {0, 255}
    ver = PV.versions(None)
    cfg = PV.model_config("j1", _params(), run, {"files": {"dem_f32.tif": {"sha256": "abc"}}}, {"schema": "national_terrain.v1", "created_at": "t"}, ver)
    assert cfg["config_hash"] == PV.config_hash({k: cfg[k] for k in cfg["hashed_keys"]})
    other = PV.model_config("j1", _params(threshold_km2=1), run, {"files": {"dem_f32.tif": {"sha256": "abc"}}}, {}, ver)
    assert other["config_hash"] != cfg["config_hash"]
    meta = PV.metadata("j1", _params(), run, cfg, files, ver, "2026-10-13T12:00:00+00:00")
    assert meta["schema"] == "geohydroai.hand_model/v1" and meta["dem"]["vertical_datum"] == "EGM2008" and len(meta["products"]) == 7
    products, dropped = PR.products_of(_params(), run, files, cfg, meta)
    assert dropped == [] and [p["kind"] for p in products] == ["hand", "slope", "dem_conditioning", "stream_links", "catchments",
                                                                 "hand_png", "hand_aux_png", "reaches", "hydrotable", "model_config", "metadata"]
    assert all(p["content_b64"] and p["file_name"] and p["processing_version"] == settings.PIPELINE_VERSION for p in products)
    assert json.loads(__import__("base64").b64decode(products[-1]["content_b64"]))["config_hash"] == cfg["config_hash"]
    assert products[0]["stats"]["hand_model"] == "m1" and products[5]["stats"]["encoding"] == "hand_rgba_v1"


def test_products_budget_drops_in_order(run, tmp_path, monkeypatch):
    from lab_service.hand import packaging as PK
    from lab_service.hand import products as PR
    from lab_service.hand import provenance as PV
    files = PK.package(run, tmp_path / "out")
    ver = PV.versions(None)
    cfg = PV.model_config("j1", _params(), run, {}, {}, ver)
    meta = PV.metadata("j1", _params(), run, cfg, files, ver, "t")
    sizes = {k: Path(f["path"]).stat().st_size for k, f in files.items()}
    jsons = {k: len(json.dumps(o, ensure_ascii=False, indent=1).encode()) for k, o in (("reaches", run["reaches"]), ("model_config", cfg), ("metadata", meta))}
    total = sum(sizes.values()) + sum(jsons.values())
    # a budget that fits everything except the first three of the drop order (the hydrotable is not counted in `total`)
    monkeypatch.setattr(settings, "HAND_EVENT_MAX_MB", (total - sizes["dem_conditioning"] - jsons["reaches"] + 1) / 2 ** 20)
    products, dropped = PR.products_of(_params(), run, files, cfg, meta)
    assert dropped[:3] == ["dem_conditioning", "hydrotable", "reaches"] and "hand" in [p["kind"] for p in products]
    assert any("size budget" in w for w in run["stats"]["quality"]["warnings"])


def test_png_budget_gives_cogs_only(run, tmp_path, monkeypatch):
    from lab_service.hand import packaging as PK
    monkeypatch.setattr(settings, "HAND_PNG_MAX_PIXELS", 10)
    files = PK.package(run, tmp_path / "out")
    assert "hand_png" not in files and run["stats"]["delivery"] == "cog_only"
    assert any("COGs only" in w for w in run["stats"]["quality"]["warnings"])


def test_rating_curves_per_reach(run):
    t = run["hydrotable"]
    ids = {r["id"] for r in run["reaches"]["reaches"]}
    assert t["schema"] == "geohydroai.hydrotable/v1" and {r["id"] for r in t["reaches"]} == ids
    for r in t["reaches"]:
        q = np.array(r["discharge_m3s"])
        assert len(q) == len(t["stages_m"]) and q[0] == 0 and (np.diff(q) >= -1e-9).all()
    assert any(max(r["discharge_m3s"]) > 0 for r in t["reaches"])
    assert "rating" in run["_stages"]
    # every reach knows where it drains; the most downstream one leaves the basin
    assert {r["to"] for r in run["reaches"]["reaches"]} <= ids | {0}
