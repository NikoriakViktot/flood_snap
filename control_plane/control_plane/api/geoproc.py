"""Frontend-compatible geoprocessing API (/v1/...).

Mirrors the Django endpoints the geoai-frontend-v2 workspace calls for basin
delineation, so the UI can be tested against the control plane directly:

  POST /v1/projects/{project_id}/basin-delineation  → 202 {job_id, basin_id, status}
  GET  /v1/jobs/{job_id}                             → Job
  GET  /v1/basin-delineations/{basin_id}             → BasinDelineation
  GET  /v1/projects/{project_id}/basin-delineations  → BasinDelineation[]
  GET  /v1/scenarios/{scenario_id}/data-products     → DataProduct[]  (latest basin, or ?basin_id=)
  GET  /v1/projects/{project_id}/data-products       → DataProduct[]
  GET  /v1/health

Everything served here is read from the worker's `manifest.json`
(/data/products/{basin_id}/manifest.json). Once Django is wired in, the same
manifest feeds `POST /internal/callbacks/job/{id}` and this router goes away.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from control_plane.domain.contracts.basin_delineation_v1 import (
    BasinDelineationPayloadV1, DEFAULT_FLOOD_LEVELS, basin_id_for,
)
from control_plane.domain.states import RunState
from control_plane.infrastructure.adapters.postgres_run_repository import PostgresRunRepository
from control_plane.infrastructure.adapters.redis_job_queue import RedisJobQueue
from control_plane.infrastructure.db.session import get_db
from control_plane.infrastructure.dispatcher import _get_redis
from control_plane.use_cases.submit_contract import SubmitContractUseCase

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1")

PRODUCTS_DIR = Path(os.getenv("PRODUCTS_DIR", "/data/products"))
# Where the browser reaches this service and the tile server (nginx / vite proxy prefixes).
PUBLIC_BASE = os.getenv("PUBLIC_BASE", "/api").rstrip("/")
TC_PUBLIC_BASE = os.getenv("TC_PUBLIC_BASE", "/tc").rstrip("/")

# UI chips → flow accumulation cells (same mapping as the frontend mock, common.ts:22)
STREAM_THRESHOLDS = {"low": 2000, "medium": 1000, "high": 500}

RUN_KEY = "basin:{basin_id}:run_id"
PROGRESS_KEY = "basin:{basin_id}:progress"


class BasinDelineationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    outlet_lon: float = Field(..., ge=-180, le=180)
    outlet_lat: float = Field(..., ge=-90, le=90)
    hand_threshold: float = Field(5.0, gt=0, le=100)
    stream_threshold: Literal["low", "medium", "high"] | int = "low"
    dem_source: Literal["copernicus_dem", "fab_dem"] = "copernicus_dem"
    snap_distance_m: float = Field(300.0, gt=0, le=5000)
    max_slope_deg: float | None = Field(8.0, gt=0, le=90)
    flood_levels_m: list[int] | None = None
    dem_bbox: list[float] | None = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _manifest_path(basin_id: str) -> Path:
    if not basin_id.isalpha():
        raise HTTPException(status_code=400, detail="invalid basin id")
    return PRODUCTS_DIR / basin_id / "manifest.json"


def _load_manifest(basin_id: str) -> dict | None:
    p = _manifest_path(basin_id)
    if not p.exists():
        return None
    with open(p) as f:
        return json.load(f)


def _iter_manifests() -> list[dict]:
    if not PRODUCTS_DIR.exists():
        return []
    out = []
    for p in PRODUCTS_DIR.glob("*/manifest.json"):
        try:
            with open(p) as f:
                out.append(json.load(f))
        except Exception:  # half-written manifest
            continue
    out.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return out


def _job_status(state: str) -> str:
    if state in (RunState.DONE, RunState.ANALYSIS_COMPLETED):
        return "done"
    if state in (RunState.FAILED, RunState.ANALYSIS_FAILED):
        return "failed"
    if state == RunState.CREATED:
        return "queued"
    return "running"


def _progress(basin_id: str) -> dict:
    raw = _get_redis().get(PROGRESS_KEY.format(basin_id=basin_id))
    if not raw:
        return {"pct": 0, "stage": "queued"}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"pct": 0, "stage": "queued"}


def _point(coords: list[float] | None) -> dict | None:
    return {"type": "Point", "coordinates": coords} if coords else None


def _basin_from_manifest(m: dict) -> dict:
    """Manifest → frontend BasinDelineation (types.ts: BasinDelineation)."""
    vectors = m.get("vectors", {})
    basin_geom = None
    gj = PRODUCTS_DIR / m["basin_id"] / vectors.get("basin", "vectors/basin.geojson")
    if gj.exists():
        with open(gj) as f:
            feat = json.load(f)
        basin_geom = feat.get("geometry", feat)
    outlet = m.get("outlet", {})
    return {
        "id": m["basin_id"],
        "project_id": m.get("project_id"),
        "job_id": m.get("run_id"),
        "job_status": "done",
        "outlet": _point(outlet.get("requested")),
        "snapped_outlet": _point(outlet.get("snapped")),
        "hand_threshold_m": m.get("parameters", {}).get("hand_threshold_m"),
        "stream_threshold": str(m.get("parameters", {}).get("stream_threshold")),
        "basin": basin_geom,
        "stats": m.get("stats"),
        "hydro": None,
        "hydro_snapshot": None,
        "created_at": m.get("created_at"),
        # extras beyond the Django shape — the full passport for modelling
        "passport": {
            "dem": m.get("dem"),
            "method": m.get("method"),
            "parameters": m.get("parameters"),
            "outlet": outlet,
            "hypsometry": m.get("hypsometry"),
            "flood_scenarios": m.get("flood_scenarios"),
            "quality": m.get("quality"),
            "software": m.get("software"),
            "files": {k: f"{PUBLIC_BASE}/files/{m['basin_id']}/{v}" for k, v in vectors.items()},
        },
    }


def _basin_pending(basin_id: str, run: dict | None, project_id: str | None) -> dict:
    payload = (run or {}).get("contract", {}).get("payload", {})
    outlet = payload.get("outlet") or {}
    status = _job_status(run["state"]) if run else "queued"
    return {
        "id": basin_id,
        "project_id": project_id or payload.get("project_id"),
        "job_id": run["id"] if run else None,
        "job_status": status,
        "outlet": _point([outlet.get("lon"), outlet.get("lat")]) if outlet else None,
        "snapped_outlet": None,
        "hand_threshold_m": payload.get("hand_threshold_m"),
        "stream_threshold": str(payload.get("stream_threshold")),
        "basin": None,
        "stats": None,
        "hydro": None,
        "hydro_snapshot": None,
        "created_at": (run or {}).get("created_at") or _now(),
    }


def _tile_url(category: str, layer: str, query: str) -> str:
    return f"{TC_PUBLIC_BASE}/singleband/{category}/{layer}/{{z}}/{{x}}/{{y}}.png?{query}&resampling=linear"


def _products_from_manifest(m: dict, project_id: str | None, scenario_id: str | None) -> list[dict]:
    """Manifest → frontend DataProduct[] (types.ts: DataProduct)."""
    basin_id = m["basin_id"]
    dem = m.get("dem", {})
    version = m.get("method", {}).get("version", "basin.delineation.v1")
    attribution = dem.get("attribution")
    out: list[dict] = []

    def product(kind: str, name: str, url: str, media_type: str = "image/png",
                stats: dict | None = None, pid: str | None = None, scen: str | None = None) -> dict:
        return {
            "id": pid or f"{basin_id}:{kind}:{name}",
            "project_id": project_id or m.get("project_id"),
            "scenario_id": scen,
            "kind": kind,
            "name": name,
            "crs": "EPSG:3857" if media_type == "image/png" else "EPSG:4326",
            "resolution_m": dem.get("grid_resolution_m"),
            "url": url,
            "media_type": media_type,
            "processing_version": version,
            "source_attribution": attribution,
            "validation_status": "internal",
            "stats": stats,
        }

    for p in m.get("products", []):
        q = f"colormap={p['colormap']}&stretch_range=[{p['stretch_range'][0]},{p['stretch_range'][1]}]"
        if p.get("explicit_color_map"):
            q = f"colormap=explicit&explicit_color_map={p['explicit_color_map']}"
        url = _tile_url(p["tc_category"], p["tc_layer"], q)
        stats = dict(p.get("stats") or {})
        if p["kind"] == "flood_depth":
            # one product with a {level} placeholder, as the UI expects
            url = url.replace(p["tc_layer"], p["tc_layer_template"])
            stats.update({
                "levels_m": m["flood_scenarios"]["levels_m"],
                "dem": dem.get("source"),
                "dem_label": dem.get("label"),
                "method": m["flood_scenarios"]["method"],
                "levels": [{k: l[k] for k in ("level_m", "area_km2", "volume_mln_m3")}
                           for l in m["flood_scenarios"]["levels"]],
            })
        out.append(product(p["kind"], p["name"], url, stats=stats or None,
                           pid=f"{basin_id}:{p['tc_layer']}"))

    # One flood_extent per scenario (the UI keys products by kind): the level nearest the requested
    # HAND threshold. The full per-level table lives in flood_depth.stats.levels and in the passport.
    fs = m.get("flood_scenarios", {})
    depth = next((p for p in m.get("products", []) if p["kind"] == "flood_depth"), None)
    levels = fs.get("levels", [])
    if levels and depth:
        want = m.get("parameters", {}).get("hand_threshold_m") or levels[0]["level_m"]
        lv = min(levels, key=lambda l: abs(l["level_m"] - want))
        q = f"colormap={depth['colormap']}&stretch_range=[{depth['stretch_range'][0]},{depth['stretch_range'][1]}]"
        url = _tile_url(depth["tc_category"], depth["tc_layer_template"].replace("{level}", str(lv["level_m"])), q)
        out.append(product(
            "flood_extent", f"HAND inundation scenario · {lv['level_m']} m", url,
            stats={
                "level_m": lv["level_m"],
                "area_km2": lv["area_km2"],
                "volume_mln_m3": lv.get("volume_mln_m3"),
                "method": fs.get("method"),
                "extent_geojson_url": f"{PUBLIC_BASE}/files/{basin_id}/{lv['extent_geojson']}" if lv.get("extent_geojson") else None,
            },
            pid=f"{basin_id}:flood_extent",
            scen=scenario_id,
        ))
    # GeoJSON vectors (basin, streams, subbasins, extents) are served via the basin passport
    # (GET /basin-delineations/{id} → passport.files), not as data products: the UI renders every
    # data product of a kind as an XYZ raster.
    return out


def _get_run(db: Session, run_id: str) -> dict | None:
    try:
        rec = PostgresRunRepository(db).get(UUID(run_id))
    except ValueError:
        return None
    if rec is None:
        return None
    return {
        "id": str(rec.id), "state": rec.state, "contract": rec.contract, "artifacts": rec.artifacts,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
    }


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------

@router.get("/health")
def health():
    return {"status": "ok", "service": "flood_control_plane", "products_dir": str(PRODUCTS_DIR)}


@router.post("/projects/{project_id}/basin-delineation", status_code=202)
def start_basin_delineation(project_id: str, body: BasinDelineationRequest, db: Session = Depends(get_db)):
    threshold = body.stream_threshold if isinstance(body.stream_threshold, int) \
        else STREAM_THRESHOLDS[body.stream_threshold]
    payload = {
        "dem_source": body.dem_source,
        "outlet": {"lon": body.outlet_lon, "lat": body.outlet_lat},
        "snap_distance_m": body.snap_distance_m,
        "stream_threshold": threshold,
        "hand_threshold_m": body.hand_threshold,
        "max_slope_deg": body.max_slope_deg,
        "flood_levels_m": body.flood_levels_m or list(DEFAULT_FLOOD_LEVELS),
        "project_id": project_id,
    }
    if body.dem_bbox:
        payload["dem_bbox"] = body.dem_bbox

    basin_id = basin_id_for(BasinDelineationPayloadV1.model_validate(payload))
    r = _get_redis()

    # Same click + same parameters → same basin: reuse the finished result.
    if _manifest_path(basin_id).exists():
        run_id = r.get(RUN_KEY.format(basin_id=basin_id)) or _load_manifest(basin_id).get("run_id")
        return {"job_id": run_id, "basin_id": basin_id, "status": "done"}

    uc = SubmitContractUseCase(PostgresRunRepository(db), RedisJobQueue(r))
    try:
        res = uc.execute({"type": "basin.delineation.v1", "spec_version": "v1", "payload": payload})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    r.set(RUN_KEY.format(basin_id=basin_id), res["run_id"])
    r.set(PROGRESS_KEY.format(basin_id=basin_id), json.dumps({"pct": 0, "stage": "queued"}))
    return {"job_id": res["run_id"], "basin_id": basin_id, "status": "queued"}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)):
    run = _get_run(db, job_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Job not found")
    payload = run["contract"].get("payload", {})
    basin_id = run["artifacts"].get("basin_id")
    if not basin_id and run["contract"].get("type") == "basin.delineation.v1":
        basin_id = basin_id_for(BasinDelineationPayloadV1.model_validate(payload))
    status = _job_status(run["state"])
    prog = _progress(basin_id) if basin_id else {"pct": 0, "stage": status}
    pct = 100 if status == "done" else int(prog.get("pct", 0))
    return {
        "id": run["id"],
        "kind": "basin_delineation",
        "status": status,
        "progress_pct": pct,
        "stage": prog.get("stage"),
        "project": payload.get("project_id"),
        "requested_by": None,
        "params": payload,
        "result_ref": basin_id,
        "error": run["artifacts"].get("error") if status == "failed" else None,
        "started_at": run["created_at"],
        "finished_at": run["updated_at"] if status in ("done", "failed") else None,
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
    }


@router.get("/basin-delineations/{basin_id}")
def get_basin(basin_id: str, db: Session = Depends(get_db)):
    m = _load_manifest(basin_id)
    if m is not None:
        return _basin_from_manifest(m)
    run_id = _get_redis().get(RUN_KEY.format(basin_id=basin_id))
    run = _get_run(db, run_id) if run_id else None
    if run is None:
        raise HTTPException(status_code=404, detail="Basin not found")
    out = _basin_pending(basin_id, run, None)
    if out["job_status"] == "failed":
        out["error"] = run["artifacts"].get("error")
    return out


@router.get("/projects/{project_id}/basin-delineations")
def list_basins(project_id: str):
    return [_basin_from_manifest(m) for m in _iter_manifests()
            if m.get("project_id") in (project_id, None)]


def _products(project_id: str | None, scenario_id: str | None, basin_id: str | None) -> list[dict]:
    if basin_id:
        m = _load_manifest(basin_id)
        if m is None:
            raise HTTPException(status_code=404, detail="Basin not found")
        return _products_from_manifest(m, project_id, scenario_id)
    manifests = _iter_manifests()
    if project_id:
        scoped = [m for m in manifests if m.get("project_id") == project_id]
        manifests = scoped or manifests
    if not manifests:
        return []
    return _products_from_manifest(manifests[0], project_id, scenario_id)


@router.get("/scenarios/{scenario_id}/data-products")
def scenario_products(scenario_id: str, basin_id: str | None = Query(None), project_id: str | None = Query(None)):
    return _products(project_id, scenario_id, basin_id)


@router.get("/projects/{project_id}/data-products")
def project_products(project_id: str, basin_id: str | None = Query(None)):
    return _products(project_id, None, basin_id)
