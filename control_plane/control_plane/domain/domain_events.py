"""Typed domain event objects emitted by workers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Union


@dataclass(frozen=True)
class SarCompleted:
    run_id: str
    tc_tif: str
    timestamp: datetime


@dataclass(frozen=True)
class DemCompleted:
    run_id: str
    hand_tif: str
    slope_tif: str
    basin_hash: str
    timestamp: datetime


@dataclass(frozen=True)
class MaskCompleted:
    run_id: str
    water_mask_tif: str
    scene: str
    timestamp: datetime


@dataclass(frozen=True)
class StepFailed:
    run_id: str
    error: str
    timestamp: datetime


@dataclass(frozen=True)
class AnalysisCompleted:
    run_id: str
    output_dir: str
    processed: list  # list of processed DEM stems
    timestamp: datetime


DomainEvent = Union[SarCompleted, DemCompleted, MaskCompleted, AnalysisCompleted, StepFailed]
