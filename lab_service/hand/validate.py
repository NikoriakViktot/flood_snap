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
            "region": district.code, "district_slug": district.slug, "district_name_ua": district.name_ua}
