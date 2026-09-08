"""Integration tests: full lifecycle through use cases with in-memory adapters.

Verifies queue contents after each step.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "control_plane"))

try:
    from control_plane.domain.states import RunState
    from control_plane.infrastructure.adapters.in_memory_run_repository import InMemoryRunRepository
    from control_plane.infrastructure.adapters.in_memory_job_queue import InMemoryJobQueue
    from control_plane.use_cases.submit_contract import SubmitContractUseCase
    from control_plane.use_cases.handle_event import HandleEventUseCase
    from control_plane.use_cases.get_run_status import GetRunStatusUseCase
    HAS_CP = True
except ImportError:
    HAS_CP = False

pytestmark = pytest.mark.skipif(not HAS_CP, reason="control_plane not available")


class TestFullLifecycleIntegration:
    def test_flood_pipeline_queue_contents(self):
        repo = InMemoryRunRepository()
        queue = InMemoryJobQueue()

        # Submit
        submit = SubmitContractUseCase(repo, queue)
        result = submit.execute({
            "type": "flood.pipeline.v1",
            "spec_version": "v1",
            "payload": {"scene": "test.SAFE", "basin_geojson": "/work/basin.geojson"},
        })
        run_id = result["run_id"]

        # Check SAR job in queue
        assert queue.size("flood_jobs") == 1
        sar_job = queue.peek("flood_jobs")[0]
        assert sar_job["scene"] == "test.SAFE"
        assert sar_job["run_id"] == run_id
        queue.pop(["flood_jobs"])

        # SAR completed
        handle = HandleEventUseCase(repo, queue)
        handle.execute(run_id, "SAR_COMPLETED", {"tc_tif": "/data/tc.tif"})

        # Check DEM job in queue
        assert queue.size("dem_jobs") == 1
        dem_job = queue.peek("dem_jobs")[0]
        assert dem_job["run_id"] == run_id
        assert dem_job["tc_tif"] == "/data/tc.tif"
        queue.pop(["dem_jobs"])

        # DEM completed
        handle.execute(run_id, "DEM_COMPLETED", {
            "hand_tif": "/data/hand.tif",
            "slope_tif": "/data/slope.tif",
            "basin_hash": "abc123",
        })

        # Check mask job in queue
        assert queue.size("mask_jobs") == 1
        mask_job = queue.peek("mask_jobs")[0]
        assert mask_job["tc_tif"] == "/data/tc.tif"
        assert mask_job["hand_tif"] == "/data/hand.tif"
        queue.pop(["mask_jobs"])

        # Mask completed
        handle.execute(run_id, "MASK_COMPLETED", {
            "water_mask_tif": "/data/mask.tif",
            "scene": "test.SAFE",
        })

        # No more jobs queued
        assert queue.size("flood_jobs") == 0
        assert queue.size("dem_jobs") == 0
        assert queue.size("mask_jobs") == 0

        # Final state
        status = GetRunStatusUseCase(repo).execute(run_id)
        assert status["state"] == RunState.DONE
        assert len(status["artifacts"]) == 6  # tc_tif, hand_tif, slope_tif, basin_hash, water_mask_tif, scene

    def test_failure_stops_pipeline(self):
        repo = InMemoryRunRepository()
        queue = InMemoryJobQueue()

        submit = SubmitContractUseCase(repo, queue)
        result = submit.execute({
            "type": "flood.pipeline.v1",
            "spec_version": "v1",
            "payload": {"scene": "test.SAFE"},
        })
        run_id = result["run_id"]
        queue.pop(["flood_jobs"])

        handle = HandleEventUseCase(repo, queue)
        result = handle.execute(run_id, "FAILED", {"error": "SNAP crashed"})

        assert result["new_state"] == RunState.FAILED
        assert result["action_dispatched"] is None
        assert queue.size("flood_jobs") == 0
        assert queue.size("dem_jobs") == 0
