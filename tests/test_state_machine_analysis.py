"""Tests for the dem_analysis bounded context in the state machine.

Verifies:
  - Analysis context transitions (happy path + failure)
  - ANALYSIS_COMPLETED / ANALYSIS_FAILED are own terminal states
  - Cross-context isolation (flood events don't affect analysis states and vice versa)
  - build_command produces StartAnalysis with the full DemAnalysisSpec
"""

import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "control_plane"))

from control_plane.domain.states import RunState
from control_plane.domain.events import EventType
from control_plane.domain.commands import StartAnalysis, Notify
from control_plane.domain.policies.run_state_machine import transition, next_action, build_command
from control_plane.domain.ports.run_repository import RunRecord

_ANALYSIS_PAYLOAD = {
    "inputs": {
        "base_dir": "/data/dems",
        "geoid_tif": "/data/egg2015.tif",
        "dem_registry": {"srtm": {"vertical_datum": "EGM96"}},
        "aoi": {},
    },
    "plan": {"crs_conversion": {"enabled": True}},
    "outputs": {"artifact_root": "/data/artifacts"},
}

_ANALYSIS_PAYLOAD_FULL = {
    "inputs": {
        "base_dir": "/data/dems",
        "geoid_tif": "/data/egg2015.tif",
        "dem_registry": {"srtm": {"vertical_datum": "EGM96"}, "alos": {"vertical_datum": "EGM96"}},
        "aoi": {"pour_point": [25.05, 47.95]},
    },
    "plan": {
        "crs_conversion": {"enabled": True},
        "terrain": {"enabled": True, "methods": ["horn"]},
        "geomorphons": {"enabled": True},
        "hydro": {"enabled": True, "stream_threshold": 300},
    },
    "outputs": {"artifact_root": "/custom/artifacts"},
}


def _run(state=RunState.ANALYSIS_RUNNING, payload=None, artifacts=None):
    return RunRecord(
        id=uuid4(),
        state=state,
        contract={"type": "dem.analysis.v1", "spec_version": "v1", "payload": payload or _ANALYSIS_PAYLOAD},
        artifacts=artifacts or {},
    )


class TestAnalysisTransitions:
    def test_analysis_happy_path_states(self):
        # Initial dispatch: state stays ANALYSIS_RUNNING, action = DEM_ANALYSIS
        assert next_action(RunState.ANALYSIS_RUNNING, EventType.CONTRACT_ACCEPTED) == "DEM_ANALYSIS"
        # Job done: ANALYSIS_RUNNING + ANALYSIS_COMPLETED → ANALYSIS_COMPLETED
        assert transition(RunState.ANALYSIS_RUNNING, EventType.ANALYSIS_COMPLETED) == RunState.ANALYSIS_COMPLETED

    def test_analysis_failure_path(self):
        assert transition(RunState.ANALYSIS_RUNNING, EventType.ANALYSIS_FAILED) == RunState.ANALYSIS_FAILED

    def test_analysis_completed_is_terminal(self):
        non_terminal_events = [
            EventType.CONTRACT_ACCEPTED, EventType.SAR_COMPLETED,
            EventType.DEM_COMPLETED, EventType.MASK_COMPLETED,
            EventType.ANALYSIS_FAILED, EventType.FAILED,
        ]
        for event in non_terminal_events:
            result = transition(RunState.ANALYSIS_COMPLETED, event)
            assert result == RunState.ANALYSIS_COMPLETED, \
                f"ANALYSIS_COMPLETED should be terminal for {event}"

    def test_analysis_failed_is_terminal_for_all_events(self):
        for event in EventType:
            result = transition(RunState.ANALYSIS_FAILED, event)
            assert result == RunState.ANALYSIS_FAILED, \
                f"ANALYSIS_FAILED should be terminal for {event}"

    def test_notify_dispatched_after_analysis_completed(self):
        assert next_action(RunState.ANALYSIS_COMPLETED, EventType.ANALYSIS_COMPLETED) == "NOTIFY"

    def test_no_action_after_analysis_failed(self):
        assert next_action(RunState.ANALYSIS_FAILED, EventType.ANALYSIS_FAILED) is None


class TestCrossContextIsolation:
    """Flood events must not affect analysis states and vice versa."""

    def test_flood_events_do_not_move_analysis_running(self):
        for event in [EventType.SAR_COMPLETED, EventType.DEM_COMPLETED, EventType.MASK_COMPLETED]:
            result = transition(RunState.ANALYSIS_RUNNING, event)
            assert result == RunState.ANALYSIS_RUNNING, \
                f"ANALYSIS_RUNNING must ignore flood event {event}"

    def test_analysis_events_do_not_move_flood_states(self):
        for flood_state in [RunState.SAR_RUNNING, RunState.DEM_RUNNING, RunState.MASK_RUNNING]:
            for analysis_event in [EventType.ANALYSIS_COMPLETED, EventType.ANALYSIS_FAILED]:
                result = transition(flood_state, analysis_event)
                assert result == flood_state, \
                    f"Flood state {flood_state} must ignore analysis event {analysis_event}"

    def test_analysis_events_do_not_move_done(self):
        assert transition(RunState.DONE, EventType.ANALYSIS_COMPLETED) == RunState.DONE
        assert transition(RunState.DONE, EventType.ANALYSIS_FAILED) == RunState.DONE

    def test_generic_failed_does_not_move_analysis_running(self):
        """Generic FAILED is a flood-context event; analysis uses ANALYSIS_FAILED."""
        result = transition(RunState.ANALYSIS_RUNNING, EventType.FAILED)
        assert result == RunState.ANALYSIS_RUNNING


class TestBuildCommandAnalysis:
    def test_returns_start_analysis(self):
        run = _run()
        cmd = build_command(RunState.ANALYSIS_RUNNING, EventType.CONTRACT_ACCEPTED, run)
        assert isinstance(cmd, StartAnalysis)
        assert cmd.run_id == str(run.id)

    def test_spec_base_dir(self):
        run = _run()
        cmd = build_command(RunState.ANALYSIS_RUNNING, EventType.CONTRACT_ACCEPTED, run)
        assert cmd.spec.inputs.base_dir == "/data/dems"

    def test_spec_geoid_tif(self):
        run = _run()
        cmd = build_command(RunState.ANALYSIS_RUNNING, EventType.CONTRACT_ACCEPTED, run)
        assert cmd.spec.inputs.geoid_tif == "/data/egg2015.tif"

    def test_spec_dem_registry(self):
        run = _run()
        cmd = build_command(RunState.ANALYSIS_RUNNING, EventType.CONTRACT_ACCEPTED, run)
        assert "srtm" in cmd.spec.inputs.dem_registry

    def test_spec_pour_point_passed(self):
        run = _run(payload=_ANALYSIS_PAYLOAD_FULL)
        cmd = build_command(RunState.ANALYSIS_RUNNING, EventType.CONTRACT_ACCEPTED, run)
        assert cmd.spec.inputs.aoi.pour_point == (25.05, 47.95)

    def test_spec_hydro_plan_passed(self):
        run = _run(payload=_ANALYSIS_PAYLOAD_FULL)
        cmd = build_command(RunState.ANALYSIS_RUNNING, EventType.CONTRACT_ACCEPTED, run)
        assert cmd.spec.plan.hydro.enabled is True
        assert cmd.spec.plan.hydro.stream_threshold == 300

    def test_spec_geomorphons_plan_passed(self):
        run = _run(payload=_ANALYSIS_PAYLOAD_FULL)
        cmd = build_command(RunState.ANALYSIS_RUNNING, EventType.CONTRACT_ACCEPTED, run)
        assert cmd.spec.plan.geomorphons.enabled is True

    def test_spec_artifact_root_passed(self):
        run = _run(payload=_ANALYSIS_PAYLOAD_FULL)
        cmd = build_command(RunState.ANALYSIS_RUNNING, EventType.CONTRACT_ACCEPTED, run)
        assert cmd.spec.outputs.artifact_root == "/custom/artifacts"

    def test_returns_none_for_analysis_failed(self):
        run = _run(state=RunState.ANALYSIS_FAILED)
        cmd = build_command(RunState.ANALYSIS_FAILED, EventType.ANALYSIS_FAILED, run)
        assert cmd is None

    def test_notify_after_analysis_completed(self):
        run = _run(state=RunState.ANALYSIS_COMPLETED,
                   artifacts={"manifest": "/data/artifacts/run1/manifest.json"})
        cmd = build_command(RunState.ANALYSIS_COMPLETED, EventType.ANALYSIS_COMPLETED, run)
        assert isinstance(cmd, Notify)
        assert "manifest" in cmd.artifacts
