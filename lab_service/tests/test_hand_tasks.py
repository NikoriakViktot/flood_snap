"""The hand_model Celery task: request validation against the terrain, events that match the v1.1 contract."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

C = Path(__file__).resolve().parents[1] / "contracts"
EVENT = json.loads((C / "job_event.v1.json").read_text())
INPUT = json.loads((C / "hand_model.v1.json").read_text())
DISTRICT = {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[24, 48], [26, 48], [26, 49], [24, 49], [24, 48]]]}}
PARAMS = {"hand_model_id": "m1", "basin_id": "b1", "outlet_lon": 25.0, "outlet_lat": 48.5, "region": "5.3.2", "dem": "fab_dem",
          "stream_threshold_km2": 1, "project_id": "p1"}


@pytest.fixture()
def lab(tmp_path, monkeypatch):
    local = tmp_path / "terrain"
    (local / "_districts").mkdir(parents=True)
    (local / "_districts" / "index.json").write_text(json.dumps([{"code": "5.3.2", "slug": "5_3_2", "name_ua": "Прут", "km2": 100}]))
    (local / "_districts" / "5_3_2.geojson").write_text(json.dumps(DISTRICT))
    for dem, staged in (("fab_dem", True), ("copernicus_dem", False)):
        (local / f"dem_source={dem}").mkdir()
        files = {f"{n}.tif": {"bytes": 1} for n in ("d8", "streams", "hand", "dem")}
        if staged:
            files["dem_f32.tif"] = {"bytes": 1, "sha256": "x"}
        (local / f"dem_source={dem}" / "manifest.json").write_text(json.dumps(
            {"schema": "national_terrain.v1", "created_at": "2026-10-12T00:00:00+00:00",
             "districts": [{"code": "5.3.2", "slug": "5_3_2", "files": files}]}))
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


def test_input_contract():
    jsonschema.validate({"job_id": "j", "params": PARAMS}, INPUT)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"job_id": "j", "params": {**PARAMS, "stream_threshold_km2": 3}}, INPUT)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"job_id": "j", "params": {**PARAMS, "dem": "srtm"}}, INPUT)


def test_prepare_hand_validates_against_the_terrain(lab):
    from lab_service import validate as V
    from lab_service.hand.validate import prepare_hand
    from lab_service.terrain import TerrainStore
    t = TerrainStore()
    p = prepare_hand(PARAMS, t)
    assert p["district_slug"] == "5_3_2" and p["stream_threshold_cells"] == 1111 and p["stream_threshold_km2"] == 1.0
    assert prepare_hand({**PARAMS, "stream_threshold_km2": 0.5}, t)["stream_threshold_cells"] == 556
    with pytest.raises(V.Rejected, match="stream_threshold_km2"):
        prepare_hand({**PARAMS, "stream_threshold_km2": 3}, t)
    with pytest.raises(V.Rejected, match="dem must be"):
        prepare_hand({**PARAMS, "dem": "srtm"}, t)
    with pytest.raises(V.Unavailable, match="Float32 DEM"):
        prepare_hand({**PARAMS, "dem": "copernicus_dem"}, t)          # listed, but dem_f32 not staged
    with pytest.raises(V.Rejected, match="outside every"):
        prepare_hand({**PARAMS, "outlet_lon": 30.0, "outlet_lat": 50.0}, t)


def test_rejected_request_is_one_failed_event(lab):
    tasks, sent = lab
    assert tasks.hand_model.run("j1", {**PARAMS, "dem": "copernicus_dem"}) == "failed"
    (name, kwargs, queue), = sent
    assert name == "platform.geoproc_event" and queue == "celery"
    _ok(kwargs)
    assert kwargs["payload"]["status"] == "failed" and "Float32 DEM" in kwargs["payload"]["error"]


def test_events_of_a_run_match_the_contract(lab, monkeypatch):
    tasks, sent = lab
    from lab_service import worker
    seen = {}

    def fake_execute(job, out_dir, report, target=None):
        seen["target"] = target
        assert job["kind"] == "hand_model" and job["params"]["district_slug"] == "5_3_2" and job["params"]["stream_threshold_cells"] == 1111
        report({"status": "running", "progress_pct": 30, "stage": "conditioning", "error": None})
        body = {"status": "done", "progress_pct": 100, "stage": "done", "error": None, "result_ref": "lab:j2",
                "result": {"kind": "hand_model", "stats": {"area_km2": 10.8}, "metadata": {"schema": "geohydroai.hand_model/v1"},
                           "config_hash": "ab" * 32, "snapped_outlet": {"type": "Point", "coordinates": [25, 48.5]}},
                "products": [{"kind": "hand", "name": "HAND · FABDEM · 1 km²", "file_name": "hand.tif", "dem": "fab_dem",
                              "media_type": "image/tiff", "processing_version": "lab.hand.v1", "crs": "EPSG:3035",
                              "license": "derived from FABDEM: CC BY-NC-SA 4.0", "content_b64": "SUkqAA=="}]}
        report(body)
        return body

    monkeypatch.setattr(worker, "execute", fake_execute)
    assert tasks.hand_model.run("j2", PARAMS) == "done"
    from lab_service.hand.runner import hand_child
    assert seen["target"] is hand_child
    assert [k["payload"]["status"] for _, k, _ in sent] == ["running", "done"]
    for _, k, _ in sent:
        _ok(k)


def test_contract_done_rules():
    hand = {"job_id": "x", "payload": {"status": "done", "result": {"kind": "hand_model", "stats": {}, "config_hash": "h", "metadata": {}}}}
    jsonschema.validate(hand, EVENT)
    with pytest.raises(jsonschema.ValidationError):            # a hand_model result needs its hash + metadata
        jsonschema.validate({"job_id": "x", "payload": {"status": "done", "result": {"kind": "hand_model", "stats": {}}}}, EVENT)
    with pytest.raises(jsonschema.ValidationError):            # a basin delineation (no kind, or kind) still needs the basin
        jsonschema.validate({"job_id": "x", "payload": {"status": "done", "result": {"stats": {}}}}, EVENT)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"job_id": "x", "payload": {"status": "done", "result": {"kind": "basin_delineation", "stats": {}}}}, EVENT)
    jsonschema.validate({"job_id": "x", "payload": {"status": "done", "result": {"kind": "basin_delineation", "basin": {}, "stats": {}}}}, EVENT)


def test_terrain_has_optional_files(lab):
    from lab_service.terrain import TerrainMissing, TerrainStore
    t = TerrainStore()
    assert t.has("fab_dem", "5_3_2", "dem_f32") and not t.has("copernicus_dem", "5_3_2", "dem_f32") and not t.has("fab_dem", "nope", "d8")
    with pytest.raises(TerrainMissing, match="not staged"):
        t.ensure("copernicus_dem", "5_3_2", "dem_f32")
