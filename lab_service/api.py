"""Django's geoprocessing contract (geohydro/adapters/gateway_geoproc_http.py) for the Flood Lab.

  POST /internal/jobs/basin-delineation   validate, queue, 202 {job_id, status: queued}
  GET  /internal/jobs/{id}                 the same body as the callback
  POST /internal/jobs/{id}/cancel          queued → cancelled (a running job cannot be interrupted → 409)
  GET  /internal/health
"""
from __future__ import annotations

import re

import redis
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from lab_service import settings
from lab_service.auth import verify_service_jwt
from lab_service.store import JobStore, public_view
from lab_service.terrain import TerrainStore

app = FastAPI(title="GeoHydroAI Flood Lab geoprocessing", docs_url=None, redoc_url=None)
store = JobStore()
terrain = TerrainStore()
_redis: redis.Redis | None = None
NATIONAL = re.compile(r"^national:([a-z_0-9]+)$")


def queue() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(settings.PLATFORM_REDIS_URL)
    return _redis


class BasinParams(BaseModel):
    job_id: str
    project_id: str | None = None
    region: str | None = Field(None, description="river-basin district code, e.g. 5.3.2")
    outlet_lon: float = Field(..., ge=-180, le=180)
    outlet_lat: float = Field(..., ge=-90, le=90)
    hand_threshold: float = 5
    stream_threshold: str = "medium"
    dem_cog_url: str = "national:fab_dem"
    callback_url: str | None = None
    callback_token: str | None = None


def resolve_dem(ref: str) -> str:
    m = NATIONAL.match(ref or "")
    if not m or m.group(1) not in settings.DEMS:
        raise HTTPException(400, f"dem_cog_url must be national:<dem> with dem in {list(settings.DEMS)}; the Lab works on the "
                                 "precomputed national terrain only")
    return m.group(1)


@app.post("/internal/jobs/basin-delineation", status_code=202, dependencies=[Depends(verify_service_jwt)])
def submit(p: BasinParams):
    primary = resolve_dem(p.dem_cog_url)
    for dem in settings.DEMS:
        if not terrain.available(dem):
            raise HTTPException(503, f"national terrain for {dem} is not published yet")
    if not p.region:
        raise HTTPException(400, "region (river-basin district code) is required: choose the district first")
    district = terrain.district_by_code(p.region)
    if district is None:
        raise HTTPException(400, f"unknown region {p.region!r}; districts: {[d.code for d in terrain.districts()]}")
    at = terrain.find_district(p.outlet_lon, p.outlet_lat)
    if at is None:
        raise HTTPException(400, "the outlet lies outside every river-basin district")
    if at.code != district.code:
        raise HTTPException(400, f"the outlet lies in district {at.code} ({at.name_ua}), not in {district.code} ({district.name_ua})")
    params = {**p.model_dump(), "primary_dem": primary, "district_slug": district.slug, "district_name_ua": district.name_ua}
    if store.load(p.job_id):
        raise HTTPException(409, f"job {p.job_id} already exists")
    store.create(p.job_id, params)
    queue().lpush(settings.QUEUE_KEY, p.job_id)
    return {"job_id": p.job_id, "status": "queued", "progress_pct": 0, "error": None, "result_ref": None}


@app.get("/internal/jobs/{job_id}", dependencies=[Depends(verify_service_jwt)])
def get_job(job_id: str):
    rec = store.load(job_id)
    if rec is None:
        raise HTTPException(404, "unknown job")
    return public_view(rec)


@app.post("/internal/jobs/{job_id}/cancel", dependencies=[Depends(verify_service_jwt)])
def cancel(job_id: str):
    rec = store.load(job_id)
    if rec is None:
        raise HTTPException(404, "unknown job")
    if rec["status"] != "queued":
        raise HTTPException(409, f"job is {rec['status']}: only a queued job can be cancelled")
    queue().lrem(settings.QUEUE_KEY, 0, job_id)
    return public_view(store.update(job_id, status="cancelled", stage="cancelled"))


@app.get("/internal/health")
def health():
    try:
        depth = queue().llen(settings.QUEUE_KEY)
        q_ok = True
    except Exception:
        depth, q_ok = None, False
    dems = {d: terrain.available(d) for d in settings.DEMS}
    return {"ok": q_ok and all(dems.values()), "dems": dems, "districts": len(terrain.districts()) if any(dems.values()) else 0,
            "queue_depth": depth, "cache_gb": round(terrain.cache_bytes() / 2**30, 2), "method": settings.METHOD}
