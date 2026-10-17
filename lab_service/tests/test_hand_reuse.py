"""Reuse of sub-basin HAND models (hand/reuse.py) on the synthetic valley of test_hand_pipeline: the basin of an outlet
at column 20 holds the sub-basin of an outlet at column 70 (and that one the sub-basin at column 90)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from lab_service import settings
from lab_service.hand import packaging as PK
from lab_service.hand import pipeline as HP
from lab_service.hand import provenance as PV
from lab_service.hand import reuse as RU
from lab_service.hand.validate import check_reuse
from lab_service.tests.test_hand_pipeline import ROW, _lonlat, _params, district, wbt  # noqa: F401  fixtures

ENTRY = {"files": {"dem_f32.tif": {"sha256": "abc"}}}
MANIFEST = {"schema": "national_terrain.v1", "created_at": "t"}


def _at(col: int, **kw):
    lon, lat = _lonlat(ROW, col)
    return _params(outlet_lon=lon, outlet_lat=lat, **kw)


def _build_sub(wbt, district, tmp_path, col: int, name: str, entry=ENTRY) -> dict:  # noqa: F811
    """A finished sub-basin model as the worker leaves it in its job folder; returns the platform's `reuse` entry."""
    p = _at(col, hand_model_id=name)
    r = HP.run_hand_model(wbt, "fab_dem", district, p, tmp_path / f"tmp_{name}")
    jd = settings.JOBS_ROOT / name
    PK.package(r, jd)
    cfg = PV.model_config(name, p, r, entry, MANIFEST, PV.versions(None))
    (jd / "model_config.json").write_text(json.dumps(cfg))
    (jd / "reaches.json").write_text(json.dumps(r["reaches"]))
    return {"hand_model_id": name, "job_id": name, "outlet": r["snapped_outlet"]["coordinates"], "cells": r["stats"]["cells"],
            "files": {}}


@pytest.fixture()
def jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "JOBS_ROOT", tmp_path / "jobs")
    return tmp_path / "jobs"


def _offer(cands, tmp_path, params):
    loaded, skipped = RU.fetch(cands, tmp_path / "fetch")
    ok, more = RU.compatible(loaded, PV.compat_config(params, ENTRY, 30.0))
    return ok, skipped + more


def test_reused_basin_equals_the_basin_computed_whole(district, wbt, tmp_path, jobs):  # noqa: F811
    whole = HP.run_hand_model(wbt, "fab_dem", district, _params(), tmp_path / "whole")
    sub = _build_sub(wbt, district, tmp_path, 70, "s70")
    ok, skipped = _offer([sub], tmp_path, _params())
    assert len(ok) == 1 and skipped == []
    r = HP.run_hand_model(wbt, "fab_dem", district, _params(), tmp_path / "reuse", reuse=ok, reuse_skipped=skipped)
    basin = whole["basin"]
    assert (r["basin"] == basin).all()
    both = basin & (whole["hand"] != HP.NODATA)
    assert (r["hand"][both] == whole["hand"][both]).all()
    assert (r["streams"] == whole["streams"]).all()
    st = r["stats"]["reuse"]
    assert [m["hand_model_id"] for m in st["models"]] == ["s70"] and st["skipped"] == []
    assert st["reused_km2"] == pytest.approx(120 * 50 * 900 / 1e6) and st["computed_km2"] == pytest.approx(120 * 50 * 900 / 1e6)
    reaches = r["reaches"]["reaches"]
    ids = [x["id"] for x in reaches]
    assert len(ids) == len(set(ids)) == 2                       # the channel split at the sub-basin's outlet
    up = next(x for x in reaches if x.get("from_model") == "s70")
    down = next(x for x in reaches if not x.get("from_model"))
    assert up["to"] == down["id"] and down["to"] == 0             # the sub-basin's reach flows into the new one
    assert set(np.unique(r["links"][r["streams"]]).tolist()) == set(ids)
    assert set(np.unique(r["catchments"][basin]).tolist()) <= set(ids)
    assert {x["id"] for x in r["hydrotable"]["reaches"]} == set(ids)
    assert [x["area_km2"] for x in r["stats"]["flood_levels"]] == [x["area_km2"] for x in whole["stats"]["flood_levels"]]
    cfg = PV.model_config("j", _params(), r, ENTRY, MANIFEST, PV.versions(None))
    assert cfg["reused_models"] == ["s70"]
    assert cfg["config_hash"] == PV.model_config("j", _params(), whole, ENTRY, MANIFEST, PV.versions(None))["config_hash"]


def test_only_the_outermost_of_nested_sub_basins_is_used(district, wbt, tmp_path, jobs):  # noqa: F811
    s70 = _build_sub(wbt, district, tmp_path, 70, "s70")
    s90 = _build_sub(wbt, district, tmp_path, 90, "s90")
    ok, skipped = _offer([s90, s70], tmp_path, _params())
    r = HP.run_hand_model(wbt, "fab_dem", district, _params(), tmp_path / "reuse", reuse=ok, reuse_skipped=skipped)
    st = r["stats"]["reuse"]
    assert [m["hand_model_id"] for m in st["models"]] == ["s70"]
    assert {"hand_model_id": "s90", "reason": "nested in another reused sub-basin"} in st["skipped"]


def test_candidates_that_do_not_fit_are_skipped_with_a_reason(district, wbt, tmp_path, jobs):  # noqa: F811
    other = _build_sub(wbt, district, tmp_path, 70, "other_terrain", entry={"files": {"dem_f32.tif": {"sha256": "old"}}})
    lost = {"hand_model_id": "lost", "job_id": "gone", "outlet": other["outlet"], "cells": 1, "files": {}}
    ok, skipped = _offer([other, lost], tmp_path, _params())
    assert ok == []
    reasons = {s["hand_model_id"]: s["reason"] for s in skipped}
    assert "dem_f32_sha256" in reasons["other_terrain"] and reasons["lost"].startswith("files:")
    # a sub-basin downstream of the new outlet is not inside the new basin
    sub = _build_sub(wbt, district, tmp_path, 10, "s10")
    ok, skipped = _offer([sub], tmp_path, _params())
    r = HP.run_hand_model(wbt, "fab_dem", district, _params(), tmp_path / "reuse", reuse=ok, reuse_skipped=skipped)
    assert r["stats"]["reuse"]["models"] == []
    assert r["stats"]["reuse"]["skipped"][0]["reason"] == "its outlet is not inside this basin"
    assert r["stats"]["reuse"]["computed_km2"] == r["stats"]["reuse"]["basin_km2"]


def test_a_cell_count_mismatch_drops_the_candidate(district, wbt, tmp_path, jobs):  # noqa: F811
    sub = _build_sub(wbt, district, tmp_path, 70, "s70")
    ok, skipped = _offer([{**sub, "cells": sub["cells"] + 5}], tmp_path, _params())
    r = HP.run_hand_model(wbt, "fab_dem", district, _params(), tmp_path / "reuse", reuse=ok, reuse_skipped=skipped)
    assert r["stats"]["reuse"]["models"] == [] and "was built on" in r["stats"]["reuse"]["skipped"][0]["reason"]


def test_footprint_is_a_wgs84_polygon(district, wbt, tmp_path):  # noqa: F811
    r = HP.run_hand_model(wbt, "fab_dem", district, _params(), tmp_path / "whole")
    fp = HP.footprint(r)
    assert fp["type"] in ("Polygon", "MultiPolygon")
    lon, lat = _lonlat(ROW, 60)
    from shapely.geometry import Point, shape
    assert shape(fp).buffer(1e-6).contains(Point(lon, lat))


def test_check_reuse_validates_the_shape():
    assert check_reuse(None) == [] and check_reuse([]) == []
    ok = check_reuse([{"hand_model_id": "a", "outlet": [25.0, 48.0], "cells": 10, "files": {"hand": "https://x/y"}}])
    assert ok[0]["outlet"] == [25.0, 48.0] and ok[0]["job_id"] is None
    from lab_service.validate import Rejected
    for bad in ([{"outlet": [1, 2]}], [{"hand_model_id": "a", "outlet": [500, 2]}],
                [{"hand_model_id": "a", "outlet": [1, 2], "files": {"hand": "file:///etc/passwd"}}],
                [{"hand_model_id": "a", "outlet": [1, 2], "cells": 0}], "x", [{}] * 65):
        with pytest.raises(Rejected):
            check_reuse(bad)
