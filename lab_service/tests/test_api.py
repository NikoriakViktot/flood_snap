"""The /internal/jobs contract: service JWT, region checks, queueing, cancel."""
from __future__ import annotations

import json
import time
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

KEY = "test-key"
DISTRICT = {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[24, 48], [26, 48], [26, 49], [24, 49], [24, 48]]]}}


class FakeRedis:
    def __init__(self):
        self.lists = {}
    def lpush(self, k, v):
        self.lists.setdefault(k, []).insert(0, v)
    def lrem(self, k, n, v):
        self.lists[k] = [x for x in self.lists.get(k, []) if x != v]
    def llen(self, k):
        return len(self.lists.get(k, []))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    local = tmp_path / "terrain"
    (local / "_districts").mkdir(parents=True)
    (local / "_districts" / "index.json").write_text(json.dumps([{"code": "5.3.2", "slug": "5_3_2", "name_ua": "Прут", "km2": 100}]))
    (local / "_districts" / "5_3_2.geojson").write_text(json.dumps(DISTRICT))
    for dem in ("fab_dem", "copernicus_dem"):
        (local / f"dem_source={dem}").mkdir()
        (local / f"dem_source={dem}" / "manifest.json").write_text(json.dumps({"districts": []}))
    monkeypatch.setenv("SERVICE_JWT_KEY", KEY)
    from lab_service import settings
    monkeypatch.setattr(settings, "SERVICE_JWT_KEY", KEY)
    monkeypatch.setattr(settings, "TERRAIN_LOCAL", str(local))
    monkeypatch.setattr(settings, "CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(settings, "JOBS_ROOT", tmp_path / "jobs")
    from lab_service import api
    from lab_service.store import JobStore
    from lab_service.terrain import TerrainStore
    api.store = JobStore(tmp_path / "jobs")
    api.terrain = TerrainStore(tmp_path / "cache")
    api._redis = FakeRedis()
    return TestClient(api.app), api


def token(aud="geoprocessing", iss="geohydro-platform", key=KEY):
    return jwt.encode({"aud": aud, "iss": iss, "exp": int(time.time()) + 300}, key, algorithm="HS256")


def body(**kw):
    return {"job_id": "11111111-1111-1111-1111-111111111111", "project_id": "p", "region": "5.3.2", "outlet_lon": 24.5, "outlet_lat": 48.5,
            "hand_threshold": 5, "stream_threshold": "medium", "dem_cog_url": "national:fab_dem",
            "callback_url": "http://platform-api:8000/cb", "callback_token": "t", **kw}


def test_auth_and_validation(client):
    c, api = client
    assert c.post("/internal/jobs/basin-delineation", json=body()).status_code == 401
    assert c.post("/internal/jobs/basin-delineation", json=body(), headers={"Authorization": f"Bearer {token(aud='tiles')}"}).status_code == 401
    h = {"Authorization": f"Bearer {token()}"}
    assert "national:<dem>" in c.post("/internal/jobs/basin-delineation", json=body(dem_cog_url="s3://x.tif"), headers=h).json()["detail"]
    assert "region" in c.post("/internal/jobs/basin-delineation", json=body(region=None), headers=h).json()["detail"]
    assert "unknown region" in c.post("/internal/jobs/basin-delineation", json=body(region="9.9.9"), headers=h).json()["detail"]
    r = c.post("/internal/jobs/basin-delineation", json=body(outlet_lon=30.0), headers=h)
    assert r.status_code == 400 and "outside every" in r.json()["detail"]


def test_submit_get_cancel(client):
    c, api = client
    h = {"Authorization": f"Bearer {token()}"}
    r = c.post("/internal/jobs/basin-delineation", json=body(), headers=h)
    assert r.status_code == 202 and r.json()["status"] == "queued"
    assert api._redis.llen("lab_jobs") == 1
    g = c.get(f"/internal/jobs/{body()['job_id']}", headers=h).json()
    assert g["status"] == "queued" and g["products"] == [] and g["result"] is None
    assert c.post("/internal/jobs/basin-delineation", json=body(), headers=h).status_code == 409
    x = c.post(f"/internal/jobs/{body()['job_id']}/cancel", headers=h).json()
    assert x["status"] == "cancelled" and api._redis.llen("lab_jobs") == 0
    assert c.post(f"/internal/jobs/{body()['job_id']}/cancel", headers=h).status_code == 409
    assert c.get("/internal/jobs/nope", headers=h).status_code == 404
    hl = c.get("/internal/health").json()
    assert hl["dems"] == {"fab_dem": True, "copernicus_dem": True} and hl["districts"] == 1
