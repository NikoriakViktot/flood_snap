"""
Contract: dem.hand.v1

Standalone DEM → HAND + slope computation via WhiteboxTools.
Can be triggered independently (e.g. to pre-cache a basin).

DEM source: Copernicus GLO-30 (primary), SNAP TC elevation (fallback).
"""

from pydantic import BaseModel, ConfigDict, Field


class DemHandPayloadV1(BaseModel):
    """Payload for dem.hand.v1 contract."""
    model_config = ConfigDict(extra="forbid")

    basin_geojson: str = Field(..., description="Path to basin boundary GeoJSON")
    tc_tif: str | None = Field(
        None,
        description="Optional: path to SNAP TC output (fallback DEM source)",
    )
    stream_threshold: int = Field(
        default=1000,
        description="Flow accumulation threshold for stream extraction",
    )
    target_epsg: str = Field(
        default="EPSG:32635",
        description="Target CRS for DEM reprojection (UTM)",
    )
    pixel_size_m: float = Field(
        default=30.0,
        description="DEM pixel size in meters",
    )
