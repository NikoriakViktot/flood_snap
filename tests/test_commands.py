"""Tests for typed commands and build_command policy."""

import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "control_plane"))

try:
    from control_plane.domain.states import RunState
    from control_plane.domain.events import EventType
    from control_plane.domain.commands import StartSar, StartDem, StartMask, Notify
    from control_plane.domain.policies.run_state_machine import build_command
    from control_plane.domain.ports.run_repository import RunRecord
    HAS_CP = True
except ImportError:
    HAS_CP = False

pytestmark = pytest.mark.skipif(not HAS_CP, reason="control_plane not available")


def _make_run(state, contract_type="flood.pipeline.v1", payload=None, artifacts=None):
    default_payload = {
        "scene": "S1B_IW_GRDH_test.SAFE",
        "basin_geojson": "/work/basin.geojson",
        "subset_wkt": None,
        "tc_epsg": "EPSG:32635",
        "stream_threshold": 1000,
    }
    return RunRecord(
        id=uuid4(),
        state=state,
        contract={
            "type": contract_type,
            "spec_version": "v1",
            "payload": payload or default_payload,
        },
        artifacts=artifacts or {},
    )


class TestBuildCommand:
    def test_build_command_returns_start_sar(self):
        run = _make_run(RunState.SAR_RUNNING)
        cmd = build_command(RunState.SAR_RUNNING, EventType.CONTRACT_ACCEPTED, run)
        assert isinstance(cmd, StartSar)
        assert cmd.scene == "S1B_IW_GRDH_test.SAFE"
        assert cmd.run_id == str(run.id)

    def test_build_command_returns_start_dem(self):
        run = _make_run(RunState.DEM_RUNNING, artifacts={"tc_tif": "/data/tc.tif"})
        cmd = build_command(RunState.DEM_RUNNING, EventType.SAR_COMPLETED, run)
        assert isinstance(cmd, StartDem)
        assert cmd.tc_tif == "/data/tc.tif"

    def test_build_command_returns_start_mask(self):
        run = _make_run(
            RunState.MASK_RUNNING,
            artifacts={
                "tc_tif": "/data/tc.tif",
                "hand_tif": "/data/hand.tif",
                "slope_tif": "/data/slope.tif",
            },
        )
        cmd = build_command(RunState.MASK_RUNNING, EventType.DEM_COMPLETED, run)
        assert isinstance(cmd, StartMask)
        assert cmd.tc_tif == "/data/tc.tif"
        assert cmd.hand_tif == "/data/hand.tif"

    def test_build_command_returns_notify(self):
        run = _make_run(
            RunState.DONE,
            artifacts={"water_mask_tif": "/data/mask.tif"},
        )
        cmd = build_command(RunState.DONE, EventType.MASK_COMPLETED, run)
        assert isinstance(cmd, Notify)
        assert cmd.artifacts == {"water_mask_tif": "/data/mask.tif"}

    def test_build_command_returns_none_on_failure(self):
        run = _make_run(RunState.FAILED)
        cmd = build_command(RunState.FAILED, EventType.FAILED, run)
        assert cmd is None
