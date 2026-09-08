"""Typed command objects dispatched to worker queues."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from control_plane.domain.specs import DemAnalysisSpec


@dataclass(frozen=True)
class StartSar:
    run_id: str
    scene: str
    basin_geojson: str
    subset_wkt: str | None = None
    tc_epsg: str = "EPSG:32635"


@dataclass(frozen=True)
class StartDem:
    run_id: str
    basin_geojson: str
    tc_tif: str | None = None
    stream_threshold: int = 1000


@dataclass(frozen=True)
class StartMask:
    run_id: str
    tc_tif: str
    hand_tif: str
    slope_tif: str
    basin_geojson: str
    output_tif: str


@dataclass(frozen=True)
class StartAnalysis:
    run_id: str
    spec: DemAnalysisSpec


@dataclass(frozen=True)
class Notify:
    run_id: str
    artifacts: dict


Command = Union[StartSar, StartDem, StartMask, StartAnalysis, Notify]
