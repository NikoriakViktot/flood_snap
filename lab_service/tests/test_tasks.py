"""The Celery transport: validation failures and results become platform.geoproc_event messages that match the
contract (contracts/job_event.v1.json); task input matches contracts/basin_delineation.v1.json."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

C = Path(__file__).resolve().parents[1] / "contracts"
EVENT = json.loads((C / "job_event.v1.json").read_text())
INPUT = json.loads((C / "basin_delineation.v1.json").read_text())
DISTRICT = {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[24, 48], [26, 48], [26, 49], [24, 49], [24, 48]]]}}


@pytest.fixture()
def lab(tmp_path, monkeypatch):
    local = tmp_path / "terrain"
    (local / "_districts").mkdir(parents=True)
    (local / "_districts" / "index.json").write_text(json.dumps([{"code": "5.3.2", "slug": "5_3_2", "name_ua": "Прут", "km2": 100}]))
    (local / "_districts" / "5_3_2.geojson").write_text(json.dumps(DISTRICT))
    for dem in ("fab_dem", "copernicus_dem"):
        (local / f"dem_source={dem}").mkdir()
        (local / f"dem_source={dem}" / "manifest.json").write_text(json.dumps({"districts": []}))
    from lab_service import settings
    monkeypatch.setattr(settings, "TERRAIN_LOCAL", str(local))
    monkeypatch.setattr(settings, "CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(settings, "JOBS_ROOT", tmp_path / "jobs")
    from lab_service import tasks
    sent = []
    monkeypatch.setattr(tasks.app, "send_task", lambda name, kwargs, queue: sent.append((name, kwargs, queue)))
    return tasks, sent


def _ok(kwargs):
    jsonschema.validate(kwargs, EVENT)


def test_rejected_request_is_one_failed_event(lab):
    tasks, sent = lab
    params = {"outlet_lon": 30.0, "outlet_lat": 50.0, "region": "5.3.2"}
    jsonschema.validate({"job_id": "j1", "params": params}, INPUT)
    assert tasks.basin_delineation.run("j1", params) == "failed"
    (name, kwargs, queue), = sent
    assert name == "platform.geoproc_event" and queue == "celery"
    _ok(kwargs)
    assert kwargs["payload"]["status"] == "failed" and "outside every" in kwargs["payload"]["error"]


def test_events_of_a_run_match_the_contract(lab, monkeypatch):
    tasks, sent = lab
    from lab_service import worker

    def fake_execute(job, out_dir, report):
        assert job["params"]["district_slug"] == "5_3_2" and job["params"]["primary_dem"] == "fab_dem"
        report({"status": "running", "progress_pct": 5, "stage": "starting", "error": None})
        body = {"status": "done", "progress_pct": 100, "stage": "done", "error": None, "result_ref": "lab:j2",
                "result": {"basin": {"type": "Polygon", "coordinates": []}, "snapped_outlet": {"type": "Point", "coordinates": [25, 48.5]},
                           "stats": {"area_km2": 1.0}}, "products": []}
        report(body)
        return body

    monkeypatch.setattr(worker, "execute", fake_execute)
    assert tasks.basin_delineation.run("j2", {"outlet_lon": 25.0, "outlet_lat": 48.5, "region": "5.3.2"}) == "done"
    assert [k["payload"]["status"] for _, k, _ in sent] == ["running", "done"]
    for _, k, _ in sent:
        _ok(k)


def test_district_files_in_epsg3035_are_matched_in_lon_lat(tmp_path, monkeypatch):
    """The staged cut lines are EPSG:3035 (as on the server); an outlet in lon/lat must still be found."""
    from pyproj import Transformer
    tr = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)
    ring = [list(tr.transform(x, y)) for x, y in [(24, 48), (26, 48), (26, 49), (24, 49), (24, 48)]]
    local = tmp_path / "terrain"
    (local / "_districts").mkdir(parents=True)
    (local / "_districts" / "index.json").write_text(json.dumps([{"code": "5.3.2", "slug": "5_3_2", "name_ua": "Прут", "km2": 100}]))
    (local / "_districts" / "5_3_2.geojson").write_text(json.dumps({
        "type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3035"}},
        "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}}]}))
    from lab_service import settings
    from lab_service.terrain import TerrainStore
    monkeypatch.setattr(settings, "TERRAIN_LOCAL", str(local))
    t = TerrainStore(tmp_path / "cache")
    assert t.find_district(24.5755, 48.4583).code == "5.3.2"
    assert t.find_district(30.52, 50.45) is None


def test_contract_rejects_a_done_event_without_result():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"job_id": "x", "payload": {"status": "done"}}, EVENT)
