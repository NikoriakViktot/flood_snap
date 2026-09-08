"""Contract schema for dem.analysis.v1 — structured inputs/plan/outputs."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, field_validator, model_validator

SUPPORTED_VCRS = {"EGM96", "EGM08", "Ellipsoid"}


# ---------------------------------------------------------------------------
# Inputs sub-models
# ---------------------------------------------------------------------------

class DemEntry(BaseModel):
    """Single DEM entry in the registry."""
    vertical_datum: Literal["EGM96", "EGM08", "Ellipsoid"]


class AoiModel(BaseModel):
    pour_point: list[float] | None = None  # [lon, lat] EPSG:4326

    @field_validator("pour_point")
    @classmethod
    def _check_coords(cls, v: list[float] | None) -> list[float] | None:
        if v is not None:
            if len(v) != 2:
                raise ValueError("pour_point must be [lon, lat]")
            lon, lat = v
            if not (-180 <= lon <= 180) or not (-90 <= lat <= 90):
                raise ValueError(f"pour_point ({lon}, {lat}) out of geographic range")
        return v


class InputsModel(BaseModel):
    base_dir: str
    geoid_tif: str
    dem_registry: dict[str, DemEntry]  # {stem: DemEntry}
    aoi: AoiModel = AoiModel()


# ---------------------------------------------------------------------------
# Plan sub-models
# ---------------------------------------------------------------------------

class CrsConversionModel(BaseModel):
    enabled: bool = True


class TerrainModel(BaseModel):
    enabled: bool = False
    methods: list[str] = ["horn"]


class GeomorphonsModel(BaseModel):
    enabled: bool = False
    search: int = 50
    threshold: float = 1.0


class HydroModel(BaseModel):
    enabled: bool = False
    stream_threshold: int = 500

    @field_validator("stream_threshold")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("stream_threshold must be > 0")
        return v


class PlanModel(BaseModel):
    crs_conversion: CrsConversionModel = CrsConversionModel()
    terrain: TerrainModel = TerrainModel()
    geomorphons: GeomorphonsModel = GeomorphonsModel()
    hydro: HydroModel = HydroModel()

    @model_validator(mode="after")
    def _crs_required_when_others_enabled(self) -> "PlanModel":
        if any([
            self.terrain.enabled,
            self.geomorphons.enabled,
            self.hydro.enabled,
        ]) and not self.crs_conversion.enabled:
            raise ValueError(
                "crs_conversion must be enabled when terrain/geomorphons/hydro are enabled"
            )
        return self


# ---------------------------------------------------------------------------
# Outputs sub-model
# ---------------------------------------------------------------------------

class OutputsModel(BaseModel):
    artifact_root: str = "/data/artifacts"


# ---------------------------------------------------------------------------
# Root payload
# ---------------------------------------------------------------------------

class DemAnalysisPayloadV1(BaseModel):
    """Payload for dem.analysis.v1 contract.

    Structure:
        inputs:  DEM sources, geoid reference, optional AOI pour point
        plan:    Which analysis steps to execute
        outputs: Where to write artifacts (default: /data/artifacts)

    All outputs land at:
        {artifact_root}/{run_id}/dem_analysis/outputs/{stem}/
    with a manifest.json at:
        {artifact_root}/{run_id}/dem_analysis/manifest.json
    """
    inputs: InputsModel
    plan: PlanModel = PlanModel()
    outputs: OutputsModel = OutputsModel()
