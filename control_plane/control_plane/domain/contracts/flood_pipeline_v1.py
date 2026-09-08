"""
Contract: flood.pipeline.v1

Full flood detection pipeline: SAR → DEM derivatives → water mask.
This is the main entry point contract.
"""

from pydantic import BaseModel, ConfigDict, Field


class FloodPipelinePayloadV1(BaseModel):
    """Payload for flood.pipeline.v1 contract."""
    model_config = ConfigDict(extra="forbid")

    scene: str = Field(..., description="Sentinel-1 .SAFE scene name")
    basin_geojson: str = Field(
        default="/work/basin_bc.geojson",
        description="Path to basin boundary GeoJSON",
    )
    subset_wkt: str | None = Field(
        None,
        description="Optional WKT polygon for SNAP Subset (defaults to basin bbox)",
    )
    tc_epsg: str = Field(
        default="EPSG:32635",
        description="CRS for Terrain Correction (UTM zone)",
    )
    stream_threshold: int = Field(
        default=1000,
        description="Flow accumulation threshold for stream extraction",
    )
    water_threshold_min: float = Field(default=0.005)
    water_threshold_max: float = Field(default=0.05)
    max_slope_deg: float = Field(default=8.0)
    max_hand_m: float = Field(default=25.0)
