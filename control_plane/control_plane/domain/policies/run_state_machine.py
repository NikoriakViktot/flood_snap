"""
Run state machine — pure functions for state transitions and command building.

Two bounded contexts, both managed here via explicit transition tables:

  flood context:    CREATED → SAR_RUNNING → DEM_RUNNING → MASK_RUNNING → DONE | FAILED
  analysis context: ANALYSIS_RUNNING → ANALYSIS_COMPLETED | ANALYSIS_FAILED

Cross-context transitions are forbidden: no ANALYSIS_* state can be reached
from a flood event, and no DONE/FAILED state is reachable from ANALYSIS_*.
"""

from __future__ import annotations

from control_plane.domain.states import RunState
from control_plane.domain.events import EventType
from control_plane.domain.commands import (
    Command, StartSar, StartDem, StartMask, StartAnalysis, Notify,
)
from control_plane.domain.specs import DemAnalysisSpec, dem_analysis_spec_from_dict
from control_plane.domain.ports.run_repository import RunRecord


# ---------------------------------------------------------------------------
# Transition table (pure, exhaustive, cross-context forbidden)
# ---------------------------------------------------------------------------

_TRANSITIONS: dict[tuple[RunState, EventType], RunState] = {
    # flood context
    (RunState.CREATED,      EventType.CONTRACT_ACCEPTED): RunState.SAR_RUNNING,
    (RunState.SAR_RUNNING,  EventType.SAR_COMPLETED):     RunState.DEM_RUNNING,
    (RunState.DEM_RUNNING,  EventType.DEM_COMPLETED):     RunState.MASK_RUNNING,
    (RunState.MASK_RUNNING, EventType.MASK_COMPLETED):    RunState.DONE,
    # flood failures
    (RunState.SAR_RUNNING,  EventType.FAILED): RunState.FAILED,
    (RunState.DEM_RUNNING,  EventType.FAILED): RunState.FAILED,
    (RunState.MASK_RUNNING, EventType.FAILED): RunState.FAILED,

    # analysis context — own terminal states (no cross-context bleeding)
    (RunState.ANALYSIS_RUNNING, EventType.ANALYSIS_COMPLETED): RunState.ANALYSIS_COMPLETED,
    (RunState.ANALYSIS_RUNNING, EventType.ANALYSIS_FAILED):    RunState.ANALYSIS_FAILED,
}

_ACTIONS: dict[tuple[RunState, EventType], str] = {
    # flood context
    (RunState.SAR_RUNNING,        EventType.CONTRACT_ACCEPTED):  "SAR_PREPROCESS",
    (RunState.DEM_RUNNING,        EventType.SAR_COMPLETED):      "DEM_COMPUTE",
    (RunState.MASK_RUNNING,       EventType.DEM_COMPLETED):      "WATER_MASK",
    (RunState.DONE,               EventType.MASK_COMPLETED):     "NOTIFY",

    # analysis context
    (RunState.ANALYSIS_RUNNING,   EventType.CONTRACT_ACCEPTED):  "DEM_ANALYSIS",
    (RunState.ANALYSIS_COMPLETED, EventType.ANALYSIS_COMPLETED): "NOTIFY",
}


def transition(current: RunState, event: EventType) -> RunState:
    """Pure state transition — returns current state if pair is not in table."""
    return _TRANSITIONS.get((current, event), current)


def next_action(state: RunState, event: EventType) -> str | None:
    """Pure function: what side-effect to dispatch after entering state."""
    return _ACTIONS.get((state, event))


# ---------------------------------------------------------------------------
# Command builder
# ---------------------------------------------------------------------------

def build_command(state: RunState, event: EventType, run: RunRecord) -> Command | None:
    """Build a typed Command from the current run state and event.

    Returns None when no command should be dispatched (FAILED / ANALYSIS_FAILED
    / unknown transition).
    """
    action = next_action(state, event)
    if action is None:
        return None

    payload = run.contract.get("payload", {})
    run_id = str(run.id)

    if action == "SAR_PREPROCESS":
        return StartSar(
            run_id=run_id,
            scene=payload["scene"],
            basin_geojson=payload.get("basin_geojson", "/work/basin_bc.geojson"),
            subset_wkt=payload.get("subset_wkt"),
            tc_epsg=payload.get("tc_epsg", "EPSG:32635"),
        )

    if action == "DEM_COMPUTE":
        return StartDem(
            run_id=run_id,
            basin_geojson=payload.get("basin_geojson", "/work/basin_bc.geojson"),
            tc_tif=run.artifacts.get("tc_tif"),
            stream_threshold=payload.get("stream_threshold", 1000),
        )

    if action == "WATER_MASK":
        return StartMask(
            run_id=run_id,
            tc_tif=run.artifacts.get("tc_tif", payload.get("tc_tif", "")),
            hand_tif=run.artifacts.get("hand_tif", payload.get("hand_tif", "")),
            slope_tif=run.artifacts.get("slope_tif", payload.get("slope_tif", "")),
            basin_geojson=payload.get("basin_geojson", "/work/basin_bc.geojson"),
            output_tif=payload.get("output_tif", f"/data/water_mask_{run_id}.tif"),
        )

    if action == "DEM_ANALYSIS":
        spec: DemAnalysisSpec = dem_analysis_spec_from_dict(payload)
        return StartAnalysis(run_id=run_id, spec=spec)

    if action == "NOTIFY":
        return Notify(run_id=run_id, artifacts=run.artifacts)

    return None
