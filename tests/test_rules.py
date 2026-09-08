"""Tests for state machine: transitions and next actions."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "control_plane"))

from control_plane.domain.states import RunState
from control_plane.domain.events import EventType
from control_plane.domain.rules import transition, next_action


class TestTransitions:
    """Test state transition function (pure)."""

    def test_full_pipeline_happy_path(self):
        """CREATED → SAR → DEM → MASK → DONE"""
        state = transition(RunState.CREATED, EventType.CONTRACT_ACCEPTED)
        assert state == RunState.SAR_RUNNING

        state = transition(RunState.SAR_RUNNING, EventType.SAR_COMPLETED)
        assert state == RunState.DEM_RUNNING

        state = transition(RunState.DEM_RUNNING, EventType.DEM_COMPLETED)
        assert state == RunState.MASK_RUNNING

        state = transition(RunState.MASK_RUNNING, EventType.MASK_COMPLETED)
        assert state == RunState.DONE

    def test_failure_from_flood_active_states(self):
        """Flood active states + FAILED → FAILED.

        Analysis context uses its own ANALYSIS_FAILED event — not the generic FAILED.
        CREATED has no active job, so FAILED is a no-op there.
        """
        flood_active = {RunState.SAR_RUNNING, RunState.DEM_RUNNING, RunState.MASK_RUNNING}
        for state in flood_active:
            result = transition(state, EventType.FAILED)
            assert result == RunState.FAILED, f"Expected FAILED from {state}"

        # Analysis context uses its own failure event
        assert transition(RunState.ANALYSIS_RUNNING, EventType.ANALYSIS_FAILED) == RunState.ANALYSIS_FAILED

        # Non-active states are no-ops for generic FAILED
        assert transition(RunState.CREATED, EventType.FAILED) == RunState.CREATED

    def test_invalid_event_stays_same(self):
        """Unrecognized event for current state → stay in current state."""
        assert transition(RunState.CREATED, EventType.DEM_COMPLETED) == RunState.CREATED
        assert transition(RunState.SAR_RUNNING, EventType.MASK_COMPLETED) == RunState.SAR_RUNNING
        assert transition(RunState.DONE, EventType.SAR_COMPLETED) == RunState.DONE

    def test_done_is_terminal(self):
        """DONE state doesn't transition on normal events."""
        for event in EventType:
            if event == EventType.FAILED:
                continue
            assert transition(RunState.DONE, event) == RunState.DONE

    def test_failed_is_terminal(self):
        """FAILED state stays FAILED for all events."""
        assert transition(RunState.FAILED, EventType.FAILED) == RunState.FAILED
        assert transition(RunState.FAILED, EventType.SAR_COMPLETED) == RunState.FAILED


class TestNextAction:
    """Test action dispatch function (pure)."""

    def test_all_pipeline_actions(self):
        """Each transition has the correct action."""
        assert next_action(RunState.SAR_RUNNING, EventType.CONTRACT_ACCEPTED) == "SAR_PREPROCESS"
        assert next_action(RunState.DEM_RUNNING, EventType.SAR_COMPLETED) == "DEM_COMPUTE"
        assert next_action(RunState.MASK_RUNNING, EventType.DEM_COMPLETED) == "WATER_MASK"
        assert next_action(RunState.DONE, EventType.MASK_COMPLETED) == "NOTIFY"

    def test_no_action_on_failure(self):
        """No action dispatched when state is FAILED."""
        assert next_action(RunState.FAILED, EventType.FAILED) is None

    def test_no_action_for_unknown_combinations(self):
        """No action for invalid state/event pairs."""
        assert next_action(RunState.CREATED, EventType.DEM_COMPLETED) is None
        assert next_action(RunState.SAR_RUNNING, EventType.MASK_COMPLETED) is None
