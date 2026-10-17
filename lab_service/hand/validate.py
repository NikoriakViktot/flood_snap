"""Validation of a hand_model request (contracts/hand_model.v1.json) against the national terrain."""
from __future__ import annotations

from lab_service import settings
from lab_service import validate as V

RES_M = 30.0


def threshold_cells(km2: float, res_m: float = RES_M) -> int:
    """Stream-initiation threshold in flow-accumulation cells (0.5 km² → 556, 1 → 1111, 2 → 2222, 5 → 5556 at 30 m)."""
    return int(round(km2 * 1e6 / (res_m * res_m)))


def prepare_hand(params: dict, terrain) -> dict:
    """The request as stored with the job: + district_slug, district_name_ua, stream_threshold_cells."""
    V.check_outlet(params)
    dem = params.get("dem")
    if dem not in settings.DEMS:
        raise V.Rejected(f"dem must be one of {list(settings.DEMS)}")
    if not terrain.available(dem):
        raise V.Unavailable(f"national terrain for {dem} is not published yet")
    thr = params.get("stream_threshold_km2")
    if isinstance(thr, bool) or not isinstance(thr, (int, float)) or not any(abs(float(thr) - t) < 1e-9 for t in settings.HAND_THRESHOLDS_KM2):
        raise V.Rejected(f"stream_threshold_km2 must be one of {[f'{t:g}' for t in settings.HAND_THRESHOLDS_KM2]} (km²)")
    district = V.check_region(params, terrain)
    if not terrain.has(dem, district.slug, "dem_f32"):
        raise V.Unavailable(f"the Float32 DEM ({dem}) is not staged for district {district.code} ({district.name_ua}) yet: "
                            "HAND models need it (scripts/national/stage_dem_f32.py)")
    return {**params, "dem": dem, "stream_threshold_km2": float(thr), "stream_threshold_cells": threshold_cells(float(thr)),
            "region": district.code, "district_slug": district.slug, "district_name_ua": district.name_ua,
            "reuse": check_reuse(params.get("reuse"))}


def check_reuse(value) -> list[dict]:
    """The offered sub-basin models (contracts/hand_model.v1.json `reuse`): shape only; hand/reuse.py checks the rest."""
    if value in (None, []):
        return []
    if not isinstance(value, list) or len(value) > 64:
        raise V.Rejected("reuse must be a list of at most 64 sub-basin models")
    out = []
    for c in value:
        if not isinstance(c, dict) or not isinstance(c.get("hand_model_id"), str):
            raise V.Rejected("reuse[]: hand_model_id is required")
        o = c.get("outlet")
        if (not isinstance(o, list) or len(o) != 2 or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in o)
                or not (-180 <= o[0] <= 180 and -90 <= o[1] <= 90)):
            raise V.Rejected("reuse[]: outlet must be [lon, lat]")
        files = c.get("files") or {}
        if not isinstance(files, dict) or not all(isinstance(u, str) and u.startswith(("https://", "http://")) for u in files.values()):
            raise V.Rejected("reuse[]: files must map product kinds to http(s) URLs")
        cells = c.get("cells")
        if cells is not None and (isinstance(cells, bool) or not isinstance(cells, int) or cells < 1):
            raise V.Rejected("reuse[]: cells must be a positive integer")
        out.append({"hand_model_id": c["hand_model_id"], "job_id": c.get("job_id"), "outlet": [float(o[0]), float(o[1])],
                    "cells": cells, "files": files})
    return out
