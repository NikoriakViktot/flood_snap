"""Integration tests: contract → state machine → dispatch → events → DONE.

Tests the full orchestration loop without real workers (Redis + mocked dispatch).
Requires: redis server (or fakeredis).
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from uuid import UUID

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "control_plane"))

# Try to import control plane modules
try:
    from control_plane.domain.states import RunState
    from control_plane.domain.events import EventType
    from control_plane.domain.rules import transition, next_action
    from control_plane.domain.contracts.validate import validate_contract
    HAS_CONTROL_PLANE = True
except ImportError:
    HAS_CONTROL_PLANE = False

pytestmark = pytest.mark.skipif(
    not HAS_CONTROL_PLANE,
    reason="control_plane package not installed",
)


class TestFullPipelineStateMachine:
    """Simulate the full pipeline state machine without DB or Redis."""

    def test_flood_pipeline_v1_lifecycle(self):
        """Simulate: contract → SAR → DEM → mask → done."""
        # 1. Validate contract
        c_type, c_ver, model = validate_contract({
            "type": "flood.pipeline.v1",
            "spec_version": "v1",
            "payload": {"scene": "S1B_IW_GRDH_xxx.SAFE"},
        })
        assert c_type == "flood.pipeline.v1"

        # 2. Initial transition: CREATED → SAR_RUNNING
        state = RunState.CREATED
        state = transition(state, EventType.CONTRACT_ACCEPTED)
        assert state == RunState.SAR_RUNNING

        action = next_action(state, EventType.CONTRACT_ACCEPTED)
        assert action == "SAR_PREPROCESS"

        # 3. SAR completes → DEM_RUNNING
        state = transition(state, EventType.SAR_COMPLETED)
        assert state == RunState.DEM_RUNNING

        action = next_action(state, EventType.SAR_COMPLETED)
        assert action == "DEM_COMPUTE"

        # 4. DEM completes → MASK_RUNNING
        state = transition(state, EventType.DEM_COMPLETED)
        assert state == RunState.MASK_RUNNING

        action = next_action(state, EventType.DEM_COMPLETED)
        assert action == "WATER_MASK"

        # 5. Mask completes → DONE
        state = transition(state, EventType.MASK_COMPLETED)
        assert state == RunState.DONE

        action = next_action(state, EventType.MASK_COMPLETED)
        assert action == "NOTIFY"

    def test_dem_hand_v1_lifecycle(self):
        """Standalone DEM contract: DEM_RUNNING → DONE."""
        c_type, _, _ = validate_contract({
            "type": "dem.hand.v1",
            "spec_version": "v1",
            "payload": {"basin_geojson": "/work/basin.geojson"},
        })
        assert c_type == "dem.hand.v1"

        # Standalone: starts at DEM_RUNNING
        state = RunState.DEM_RUNNING

        # DEM completes → MASK_RUNNING (but for standalone, this is the final step)
        state = transition(state, EventType.DEM_COMPLETED)
        assert state == RunState.MASK_RUNNING

    def test_failure_at_any_stage(self):
        """FAILED event at any stage → FAILED, no further actions."""
        stages = [
            (RunState.SAR_RUNNING, EventType.CONTRACT_ACCEPTED),
            (RunState.DEM_RUNNING, EventType.SAR_COMPLETED),
            (RunState.MASK_RUNNING, EventType.DEM_COMPLETED),
        ]

        for initial_state, _ in stages:
            failed = transition(initial_state, EventType.FAILED)
            assert failed == RunState.FAILED
            assert next_action(failed, EventType.FAILED) is None


class TestDispatcherMapping:
    """Test that dispatcher maps actions to correct Redis queues."""

    @patch("control_plane.infrastructure.dispatcher._get_redis")
    def test_sar_preprocess_goes_to_flood_jobs(self, mock_redis_fn):
        from control_plane.infrastructure.dispatcher import dispatch
        from uuid import uuid4

        mock_r = MagicMock()
        mock_redis_fn.return_value = mock_r

        dispatch("SAR_PREPROCESS", uuid4(), contract_payload={"scene": "test.SAFE"})
        mock_r.rpush.assert_called_once()
        queue_name = mock_r.rpush.call_args[0][0]
        assert queue_name == "flood_jobs"

    @patch("control_plane.infrastructure.dispatcher._get_redis")
    def test_dem_compute_goes_to_dem_jobs(self, mock_redis_fn):
        from control_plane.infrastructure.dispatcher import dispatch
        from uuid import uuid4

        mock_r = MagicMock()
        mock_redis_fn.return_value = mock_r

        dispatch("DEM_COMPUTE", uuid4(), contract_payload={"basin_geojson": "/work/b.geojson"})
        mock_r.rpush.assert_called_once()
        queue_name = mock_r.rpush.call_args[0][0]
        assert queue_name == "dem_jobs"

    @patch("control_plane.infrastructure.dispatcher._get_redis")
    def test_water_mask_goes_to_mask_jobs(self, mock_redis_fn):
        from control_plane.infrastructure.dispatcher import dispatch
        from uuid import uuid4

        mock_r = MagicMock()
        mock_redis_fn.return_value = mock_r

        dispatch("WATER_MASK", uuid4(), contract_payload={"tc_tif": "/data/tc.tif"})
        mock_r.rpush.assert_called_once()
        queue_name = mock_r.rpush.call_args[0][0]
        assert queue_name == "mask_jobs"

    @patch("control_plane.infrastructure.dispatcher._get_redis")
    def test_notify_does_not_push_to_queue(self, mock_redis_fn):
        from control_plane.infrastructure.dispatcher import dispatch
        from uuid import uuid4

        mock_r = MagicMock()
        mock_redis_fn.return_value = mock_r

        dispatch("NOTIFY", uuid4(), artifacts={"water_mask_tif": "/data/mask.tif"})
        mock_r.rpush.assert_not_called()


class TestArtifactAccumulation:
    """Test that artifacts accumulate correctly through the pipeline."""

    def test_artifacts_merge(self):
        """Each event adds artifacts; previous ones are preserved."""
        artifacts = {}

        # SAR_COMPLETED adds tc_tif
        sar_artifacts = {"tc_tif": "/data/_tc_xxx.tif"}
        artifacts = {**artifacts, **sar_artifacts}
        assert "tc_tif" in artifacts

        # DEM_COMPLETED adds hand_tif, slope_tif
        dem_artifacts = {
            "hand_tif": "/data/aux/abc123/hand.tif",
            "slope_tif": "/data/aux/abc123/slope.tif",
            "basin_hash": "abc123def456",
        }
        artifacts = {**artifacts, **dem_artifacts}
        assert "tc_tif" in artifacts      # preserved
        assert "hand_tif" in artifacts    # added
        assert "slope_tif" in artifacts   # added

        # MASK_COMPLETED adds water_mask_tif
        mask_artifacts = {
            "water_mask_tif": "/data/water_mask_xxx.tif",
            "scene": "S1B_IW_GRDH_xxx.SAFE",
        }
        artifacts = {**artifacts, **mask_artifacts}
        assert len(artifacts) == 6  # all artifacts present


class TestContractPayloadPropagation:
    """Test that contract payload fields propagate to dispatched jobs."""

    @patch("control_plane.infrastructure.dispatcher._get_redis")
    def test_scene_in_flood_job(self, mock_redis_fn):
        from control_plane.infrastructure.dispatcher import dispatch
        from uuid import uuid4

        mock_r = MagicMock()
        mock_redis_fn.return_value = mock_r

        payload = {
            "scene": "S1B_IW_GRDH_test.SAFE",
            "basin_geojson": "/work/basin.geojson",
        }
        dispatch("SAR_PREPROCESS", uuid4(), contract_payload=payload)

        pushed_json = mock_r.rpush.call_args[0][1]
        pushed = json.loads(pushed_json)
        assert pushed["scene"] == "S1B_IW_GRDH_test.SAFE"
        assert pushed["basin_geojson"] == "/work/basin.geojson"
        assert "run_id" in pushed
