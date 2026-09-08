"""
Typed value-object hierarchy for dem.analysis.v1 processing spec.

This is the single structured type accepted by StartAnalysis, replacing
the previous "optional fields everywhere" pattern.

Usage:
    spec = DemAnalysisSpec(
        inputs=InputsSpec(
            base_dir="/data/dems",
            geoid_tif="/data/egg2015.tif",
            dem_registry={"srtm": {"vertical_datum": "EGM96"}},
            aoi=AoiSpec(pour_point=(25.05, 47.95)),
        ),
        plan=PlanSpec(
            crs_conversion=CrsConversionSpec(enabled=True),
            terrain=TerrainSpec(enabled=True),
            geomorphons=GeomorphonsSpec(enabled=True),
            hydro=HydroSpec(enabled=True, stream_threshold=300),
        ),
        outputs=OutputsSpec(artifact_root="/data/artifacts"),
    )

Serialization: dataclasses.asdict(spec) → plain dict → JSON-safe.
Deserialisation: dem_analysis_spec_from_dict(d) → DemAnalysisSpec.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AoiSpec:
    """Area-of-interest for optional watershed delineation."""
    pour_point: Optional[tuple[float, float]] = None  # (lon, lat) EPSG:4326


@dataclass(frozen=True)
class InputsSpec:
    """Input resources for the analysis."""
    base_dir: str
    geoid_tif: str
    dem_registry: dict  # {stem: {"vertical_datum": "EGM96"|"EGM08"|"Ellipsoid"}}
    aoi: AoiSpec = field(default_factory=AoiSpec)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CrsConversionSpec:
    """xDEM vertical CRS conversion — always runs."""
    enabled: bool = True


@dataclass(frozen=True)
class TerrainSpec:
    """xDEM terrain attribute computation."""
    enabled: bool = False
    methods: tuple = ("horn",)   # slope method(s)


@dataclass(frozen=True)
class GeomorphonsSpec:
    """WhiteboxTools Geomorphons landform classification."""
    enabled: bool = False
    search: int = 50             # lookup radius in cells
    threshold: float = 1.0      # flatness threshold in degrees


@dataclass(frozen=True)
class HydroSpec:
    """WhiteboxTools full hydrological pipeline."""
    enabled: bool = False
    stream_threshold: int = 500  # flow-accumulation cells to define streams


@dataclass(frozen=True)
class PlanSpec:
    """What analysis steps to execute."""
    crs_conversion: CrsConversionSpec = field(default_factory=CrsConversionSpec)
    terrain: TerrainSpec = field(default_factory=TerrainSpec)
    geomorphons: GeomorphonsSpec = field(default_factory=GeomorphonsSpec)
    hydro: HydroSpec = field(default_factory=HydroSpec)


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OutputsSpec:
    """Where artifacts are written.  All outputs land under:
        {artifact_root}/{run_id}/dem_analysis/
    """
    artifact_root: str = "/data/artifacts"


# ---------------------------------------------------------------------------
# Root spec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DemAnalysisSpec:
    """Single value-object passed to StartAnalysis command."""
    inputs: InputsSpec
    plan: PlanSpec
    outputs: OutputsSpec

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Deserialisation helper (dict → DemAnalysisSpec)
# ---------------------------------------------------------------------------

def dem_analysis_spec_from_dict(d: dict) -> DemAnalysisSpec:
    """Reconstruct DemAnalysisSpec from a plain dict (e.g. from Redis payload)."""
    inp = d["inputs"]
    pln = d.get("plan", {})
    out = d.get("outputs", {})

    aoi_raw = inp.get("aoi", {})
    pp_raw = aoi_raw.get("pour_point")
    aoi = AoiSpec(pour_point=tuple(pp_raw) if pp_raw else None)

    terrain_raw = pln.get("terrain", {})
    geomorphons_raw = pln.get("geomorphons", {})
    hydro_raw = pln.get("hydro", {})
    crs_raw = pln.get("crs_conversion", {})

    return DemAnalysisSpec(
        inputs=InputsSpec(
            base_dir=inp["base_dir"],
            geoid_tif=inp["geoid_tif"],
            dem_registry=inp["dem_registry"],
            aoi=aoi,
        ),
        plan=PlanSpec(
            crs_conversion=CrsConversionSpec(
                enabled=crs_raw.get("enabled", True),
            ),
            terrain=TerrainSpec(
                enabled=terrain_raw.get("enabled", False),
                methods=tuple(terrain_raw.get("methods", ["horn"])),
            ),
            geomorphons=GeomorphonsSpec(
                enabled=geomorphons_raw.get("enabled", False),
                search=geomorphons_raw.get("search", 50),
                threshold=geomorphons_raw.get("threshold", 1.0),
            ),
            hydro=HydroSpec(
                enabled=hydro_raw.get("enabled", False),
                stream_threshold=hydro_raw.get("stream_threshold", 500),
            ),
        ),
        outputs=OutputsSpec(
            artifact_root=out.get("artifact_root", "/data/artifacts"),
        ),
    )
