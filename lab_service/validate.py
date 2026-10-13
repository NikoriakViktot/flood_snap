"""Validation of a basin-delineation request against the national terrain (shared by the Celery task and the
legacy HTTP API). Raises Rejected (the request is wrong: the job fails with this message) or Unavailable (the terrain
is not published: retry later)."""
from __future__ import annotations

import re

from lab_service import settings

NATIONAL = re.compile(r"^national:([a-z_0-9]+)$")


class Rejected(ValueError):
    pass


class Unavailable(RuntimeError):
    pass


def resolve_dem(ref: str) -> str:
    m = NATIONAL.match(ref or "")
    if not m or m.group(1) not in settings.DEMS:
        raise Rejected(f"dem_cog_url must be national:<dem> with dem in {list(settings.DEMS)}; the Lab works on the "
                       "precomputed national terrain only")
    return m.group(1)


def prepare(params: dict, terrain) -> dict:
    """The request as stored with the job: + primary_dem, district_slug, district_name_ua."""
    for k in ("outlet_lon", "outlet_lat"):
        if not isinstance(params.get(k), (int, float)):
            raise Rejected(f"{k} is required (number, EPSG:4326)")
    if not (-180 <= params["outlet_lon"] <= 180 and -90 <= params["outlet_lat"] <= 90):
        raise Rejected("outlet out of range")
    primary = resolve_dem(params.get("dem_cog_url") or "national:fab_dem")
    for dem in settings.DEMS:
        if not terrain.available(dem):
            raise Unavailable(f"national terrain for {dem} is not published yet")
    region = params.get("region")
    if not region:
        raise Rejected("region (river-basin district code) is required: choose the district first")
    district = terrain.district_by_code(region)
    if district is None:
        raise Rejected(f"unknown region {region!r}; districts: {[d.code for d in terrain.districts()]}")
    at = terrain.find_district(params["outlet_lon"], params["outlet_lat"])
    if at is None:
        raise Rejected("the outlet lies outside every river-basin district")
    if at.code != district.code:
        raise Rejected(f"the outlet lies in district {at.code} ({at.name_ua}), not in {district.code} ({district.name_ua})")
    return {"hand_threshold": 5, "stream_threshold": "medium", **params, "primary_dem": primary,
            "district_slug": district.slug, "district_name_ua": district.name_ua}
