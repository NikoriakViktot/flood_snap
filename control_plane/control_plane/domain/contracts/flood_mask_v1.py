"""
Contract: flood.mask.v1

Standalone water mask computation.
Requires pre-computed HAND + slope (from DEM worker) + TC output (from SAR worker).
"""

from pydantic import BaseModel, ConfigDict, Field


class FloodMaskPayloadV1(BaseModel):
    """Payload for flood.mask.v1 contract."""
    model_config = ConfigDict(extra="forbid")

    tc_tif: str = Field(..., description="Path to SNAP TC output (Sigma0 + elevation)")
    hand_tif: str = Field(..., description="Path to HAND raster")
    slope_tif: str = Field(..., description="Path to slope raster (degrees)")
    basin_geojson: str = Field(..., description="Path to basin boundary GeoJSON")
    output_tif: str = Field(..., description="Path for output water mask")
    water_threshold_min: float = Field(default=0.005)
    water_threshold_max: float = Field(default=0.05)
    max_slope_deg: float = Field(default=8.0)
    max_hand_m: float = Field(default=25.0)
