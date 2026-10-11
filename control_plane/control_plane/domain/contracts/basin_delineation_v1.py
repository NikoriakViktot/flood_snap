"""
Contract: basin.delineation.v1

Click-to-basin: snap an outlet to the drainage network, delineate the watershed
(WhiteboxTools D8), compute a reproducible hydrological basin passport and
HAND inundation scenarios, publish rasters as COGs to Terracotta.

The regional terrain (DEM → breach → D8 → accumulation → streams → HAND → slope)
is cached per (dem_source, dem_bbox, target_epsg, pixel_size_m); only the
per-outlet steps run on every click.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DemSource = Literal["copernicus_dem", "fab_dem"]
DEFAULT_FLOOD_LEVELS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


class OutletV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lon: float = Field(..., ge=-180, le=180)
    lat: float = Field(..., ge=-90, le=90)


class BasinDelineationPayloadV1(BaseModel):
    """Payload for basin.delineation.v1 contract."""
    model_config = ConfigDict(extra="forbid")

    dem_source: DemSource = Field(
        "copernicus_dem",
        description="DEM used for hydrological conditioning (copernicus_dem = GLO-30, fab_dem = FABDEM)",
    )
    outlet: OutletV1 = Field(..., description="Requested outlet, WGS84")
    snap_distance_m: float = Field(300.0, gt=0, le=5000,
                                   description="Max distance to move the outlet onto a stream cell")
    stream_threshold: int = Field(2000, ge=50, le=1_000_000,
                                  description="Flow accumulation (cells) that defines a stream")
    hand_threshold_m: float = Field(5.0, gt=0, le=100,
                                    description="HAND level the UI treats as the screening threshold")
    max_slope_deg: float | None = Field(
        8.0, gt=0, le=90,
        description="HAND scenarios: cells steeper than this are not inundated (stream cells always are); "
                    "null disables. 8° matches the SAR compound mask.",
    )
    flood_levels_m: list[int] = Field(
        default_factory=lambda: list(DEFAULT_FLOOD_LEVELS),
        description="HAND inundation scenario levels (m above nearest drainage)",
    )
    dem_bbox: list[float] | None = Field(
        None,
        description="Regional DEM extent [west, south, east, north] WGS84; worker default when null",
    )
    target_epsg: str = Field("EPSG:32635", description="Processing CRS (projected, metres)")
    pixel_size_m: float = Field(30.0, ge=5, le=500)
    cn_grid_tif: str | None = Field(None, description="Optional SCS curve-number raster for lag time")
    project_id: str | None = Field(None, description="Echoed into the manifest for the frontend")

    @field_validator("flood_levels_m")
    @classmethod
    def _levels(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("flood_levels_m must not be empty")
        if len(set(v)) != len(v):
            raise ValueError("flood_levels_m must be unique")
        if any(l < 1 or l > 50 for l in v):
            raise ValueError("flood_levels_m must be within 1..50 m")
        return sorted(v)

    @field_validator("dem_bbox")
    @classmethod
    def _bbox(cls, v: list[float] | None) -> list[float] | None:
        if v is None:
            return v
        if len(v) != 4 or not (v[0] < v[2] and v[1] < v[3]):
            raise ValueError("dem_bbox must be [west, south, east, north] with west<east, south<north")
        return v

    @field_validator("target_epsg")
    @classmethod
    def _epsg(cls, v: str) -> str:
        if not v.upper().startswith("EPSG:") or v.upper() == "EPSG:4326":
            raise ValueError("target_epsg must be a projected EPSG code (not EPSG:4326)")
        return v.upper()


_ALPHABET = "abcdefghijklmnopqrstuvwxyz"


def basin_id_for(payload: BasinDelineationPayloadV1) -> str:
    """Deterministic, letters-only basin id.

    Same outlet (to 1e-5°) + same processing parameters → same id, so repeated
    clicks hit the cache. Letters only: the frontend rewrites tile URLs with
    `replaceAll("2000", N)`, so ids must never contain digits.
    """
    key = {
        "dem_source": payload.dem_source,
        "lon": round(payload.outlet.lon, 5),
        "lat": round(payload.outlet.lat, 5),
        "snap": payload.snap_distance_m,
        "stream": payload.stream_threshold,
        "hand": payload.hand_threshold_m,
        "max_slope": payload.max_slope_deg,
        "levels": payload.flood_levels_m,
        "bbox": payload.dem_bbox,
        "epsg": payload.target_epsg,
        "res": payload.pixel_size_m,
    }
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).digest()
    n = int.from_bytes(digest[:16], "big")
    out = []
    for _ in range(12):
        n, r = divmod(n, 26)
        out.append(_ALPHABET[r])
    return "b" + "".join(out)
