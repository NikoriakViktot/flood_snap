"""Validation of a subbasin_delineation request (contracts/subbasin_delineation.v1.json) against the national terrain."""
from __future__ import annotations

from lab_service import settings
from lab_service import validate as V


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def check_gauges(value) -> list[dict]:
    if value in (None, []):
        return []
    if not isinstance(value, list) or len(value) > settings.SUBBASIN_MAX_GAUGES:
        raise V.Rejected(f"gauges must be a list of at most {settings.SUBBASIN_MAX_GAUGES} posts")
    out = []
    for g in value:
        if not isinstance(g, dict) or not isinstance(g.get("post_id"), int) or isinstance(g.get("post_id"), bool):
            raise V.Rejected("gauges[]: post_id (integer) is required")
        if not (_num(g.get("lon")) and _num(g.get("lat")) and -180 <= g["lon"] <= 180 and -90 <= g["lat"] <= 90):
            raise V.Rejected(f"gauges[] {g['post_id']}: lon/lat (EPSG:4326) are required")
        a = g.get("area_km2")
        if a is not None and (not _num(a) or a <= 0):
            raise V.Rejected(f"gauges[] {g['post_id']}: area_km2 must be a positive number or null")
        out.append({"post_id": g["post_id"], "lon": float(g["lon"]), "lat": float(g["lat"]),
                    "area_km2": float(a) if a is not None else None, "name": g.get("name")})
    return out


def check_rivers(value) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("type") != "FeatureCollection" or not isinstance(value.get("features"), list):
        raise V.Rejected("rivers must be a GeoJSON FeatureCollection of lines (EPSG:4326)")
    if len(value["features"]) > settings.SUBBASIN_MAX_RIVER_LINES:
        raise V.Rejected(f"rivers: at most {settings.SUBBASIN_MAX_RIVER_LINES} lines")
    return value


def prepare_subbasins(params: dict, terrain) -> dict:
    """The request as stored with the job: + district_slug, district_name_ua, the defaults applied."""
    V.check_outlet(params)
    dem = params.get("dem") or settings.PRIMARY_DEM
    if dem not in settings.DEMS:
        raise V.Rejected(f"dem must be one of {list(settings.DEMS)}")
    if not terrain.available(dem):
        raise V.Unavailable(f"national terrain for {dem} is not published yet")
    thr = params.get("stream_threshold_km2", settings.SUBBASIN_DEFAULT_THRESHOLD_KM2)
    lo, hi = settings.SUBBASIN_THRESHOLD_KM2
    if not _num(thr) or not lo <= float(thr) <= hi:
        raise V.Rejected(f"stream_threshold_km2 must be a number in {lo:g}…{hi:g} km²")
    burn = params.get("burn_depth_m", settings.SUBBASIN_BURN_DEPTH_M)
    if not _num(burn) or not 0 <= float(burn) <= 100:
        raise V.Rejected("burn_depth_m must be a number in 0…100 m (0 = no burn-in)")
    district = V.check_region(params, terrain)
    if not terrain.has(dem, district.slug, "dem_f32"):
        raise V.Unavailable(f"the Float32 DEM ({dem}) is not staged for district {district.code} ({district.name_ua}) yet")
    return {**params, "dem": dem, "stream_threshold_km2": float(thr), "burn_depth_m": float(burn),
            "region": district.code, "district_slug": district.slug, "district_name_ua": district.name_ua,
            "gauges": check_gauges(params.get("gauges")), "rivers": check_rivers(params.get("rivers"))}
