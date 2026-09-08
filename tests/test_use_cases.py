"""Tests for use cases with in-memory adapters."""

import sys
from pathlib import Path
from uuid import UUID

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "control_plane"))

try:
    from control_plane.domain.states import RunState
    from control_plane.domain.events import EventType
    from control_plane.infrastructure.adapters.in_memory_run_repository import InMemoryRunRepository
    from control_plane.infrastructure.adapters.in_memory_job_queue import InMemoryJobQueue
    from control_plane.use_cases.submit_contract import SubmitContractUseCase
    from control_plane.use_cases.handle_event import HandleEventUseCase
    from control_plane.use_cases.get_run_status import GetRunStatusUseCase
    HAS_CP = True
except ImportError:
    HAS_CP = False

pytestmark = pytest.mark.skipif(not HAS_CP, reason="control_plane not available")


@pytest.fixture
def repo():
    return InMemoryRunRepository()


@pytest.fixture
def queue():
    return InMemoryJobQueue()


class TestSubmitContractUseCase:
    def test_submit_flood_pipeline_creates_run_and_dispatches_sar(self, repo, queue):
        uc = SubmitContractUseCase(repo, queue)
        result = uc.execute({
            "type": "flood.pipeline.v1",
            "spec_version": "v1",
            "payload": {"scene": "S1B_IW_GRDH_test.SAFE"},
        })
        assert result["state"] == RunState.SAR_RUNNING
        assert result["dispatched_action"] == "StartSar"
        assert queue.size("flood_jobs") == 1

        job = queue.pop(["flood_jobs"])[1]
        assert job["scene"] == "S1B_IW_GRDH_test.SAFE"

    def test_submit_dem_hand_starts_at_dem_running(self, repo, queue):
        uc = SubmitContractUseCase(repo, queue)
        result = uc.execute({
            "type": "dem.hand.v1",
            "spec_version": "v1",
            "payload": {"basin_geojson": "/work/basin.geojson"},
        })
        assert result["state"] == RunState.DEM_RUNNING
        assert result["dispatched_action"] == "StartDem"
        assert queue.size("dem_jobs") == 1

    def test_submit_flood_mask_starts_at_mask_running(self, repo, queue):
        uc = SubmitContractUseCase(repo, queue)
        result = uc.execute({
            "type": "flood.mask.v1",
            "spec_version": "v1",
            "payload": {
                "tc_tif": "/data/tc.tif",
                "hand_tif": "/data/hand.tif",
                "slope_tif": "/data/slope.tif",
                "basin_geojson": "/work/basin.geojson",
                "output_tif": "/data/mask.tif",
            },
        })
        assert result["state"] == RunState.MASK_RUNNING
        assert result["dispatched_action"] == "StartMask"
        assert queue.size("mask_jobs") == 1

    def test_submit_invalid_contract_raises(self, repo, queue):
        uc = SubmitContractUseCase(repo, queue)
        with pytest.raises(ValueError):
            uc.execute({"type": "nonexistent", "spec_version": "v1", "payload": {}})


class TestHandleEventUseCase:
    def _submit_flood(self, repo, queue):
        uc = SubmitContractUseCase(repo, queue)
        result = uc.execute({
            "type": "flood.pipeline.v1",
            "spec_version": "v1",
            "payload": {"scene": "S1B_IW_GRDH_test.SAFE"},
        })
        # Drain the SAR job
        queue.pop(["flood_jobs"])
        return result["run_id"]

    def test_handle_sar_completed_transitions_to_dem_running(self, repo, queue):
        run_id = self._submit_flood(repo, queue)
        uc = HandleEventUseCase(repo, queue)
        result = uc.execute(run_id, "SAR_COMPLETED", {"tc_tif": "/data/tc.tif"})
        assert result["new_state"] == RunState.DEM_RUNNING
        assert result["action_dispatched"] == "StartDem"
        assert queue.size("dem_jobs") == 1

    def test_handle_dem_completed_dispatches_mask_job(self, repo, queue):
        run_id = self._submit_flood(repo, queue)
        uc = HandleEventUseCase(repo, queue)
        uc.execute(run_id, "SAR_COMPLETED", {"tc_tif": "/data/tc.tif"})
        queue.pop(["dem_jobs"])
        result = uc.execute(run_id, "DEM_COMPLETED", {
            "hand_tif": "/data/hand.tif",
            "slope_tif": "/data/slope.tif",
            "basin_hash": "abc123def456",
        })
        assert result["new_state"] == RunState.MASK_RUNNING
        assert result["action_dispatched"] == "StartMask"
        assert queue.size("mask_jobs") == 1

    def test_handle_mask_completed_transitions_to_done(self, repo, queue):
        run_id = self._submit_flood(repo, queue)
        uc = HandleEventUseCase(repo, queue)
        uc.execute(run_id, "SAR_COMPLETED", {"tc_tif": "/data/tc.tif"})
        queue.pop(["dem_jobs"])
        uc.execute(run_id, "DEM_COMPLETED", {
            "hand_tif": "/data/hand.tif",
            "slope_tif": "/data/slope.tif",
        })
        queue.pop(["mask_jobs"])
        result = uc.execute(run_id, "MASK_COMPLETED", {
            "water_mask_tif": "/data/mask.tif",
            "scene": "S1B_IW_GRDH_test.SAFE",
        })
        assert result["new_state"] == RunState.DONE
        assert result["action_dispatched"] == "Notify"

    def test_handle_event_unknown_run_raises(self, repo, queue):
        uc = HandleEventUseCase(repo, queue)
        with pytest.raises(LookupError):
            uc.execute("00000000-0000-0000-0000-000000000000", "SAR_COMPLETED", {})


class TestGetRunStatusUseCase:
    def test_get_existing_run(self, repo, queue):
        submit = SubmitContractUseCase(repo, queue)
        result = submit.execute({
            "type": "flood.pipeline.v1",
            "spec_version": "v1",
            "payload": {"scene": "test.SAFE"},
        })
        uc = GetRunStatusUseCase(repo)
        status = uc.execute(result["run_id"])
        assert status is not None
        assert status["state"] == RunState.SAR_RUNNING

    def test_get_nonexistent_run(self, repo):
        uc = GetRunStatusUseCase(repo)
        assert uc.execute("00000000-0000-0000-0000-000000000000") is None

    def test_list_runs(self, repo, queue):
        submit = SubmitContractUseCase(repo, queue)
        for _ in range(3):
            submit.execute({
                "type": "flood.pipeline.v1",
                "spec_version": "v1",
                "payload": {"scene": "test.SAFE"},
            })
        uc = GetRunStatusUseCase(repo)
        runs = uc.list()
        assert len(runs) == 3


class TestFullLifecycle:
    def test_full_lifecycle(self, repo, queue):
        """Submit → SAR → DEM → Mask → DONE."""
        # 1. Submit
        submit = SubmitContractUseCase(repo, queue)
        result = submit.execute({
            "type": "flood.pipeline.v1",
            "spec_version": "v1",
            "payload": {"scene": "S1B_IW_GRDH_test.SAFE"},
        })
        run_id = result["run_id"]
        assert result["state"] == RunState.SAR_RUNNING
        assert queue.size("flood_jobs") == 1
        queue.pop(["flood_jobs"])

        handle = HandleEventUseCase(repo, queue)

        # 2. SAR completed
        result = handle.execute(run_id, "SAR_COMPLETED", {"tc_tif": "/data/tc.tif"})
        assert result["new_state"] == RunState.DEM_RUNNING
        assert queue.size("dem_jobs") == 1
        queue.pop(["dem_jobs"])

        # 3. DEM completed
        result = handle.execute(run_id, "DEM_COMPLETED", {
            "hand_tif": "/data/hand.tif",
            "slope_tif": "/data/slope.tif",
            "basin_hash": "abc123def456",
        })
        assert result["new_state"] == RunState.MASK_RUNNING
        assert queue.size("mask_jobs") == 1
        queue.pop(["mask_jobs"])

        # 4. Mask completed
        result = handle.execute(run_id, "MASK_COMPLETED", {
            "water_mask_tif": "/data/mask.tif",
            "scene": "S1B_IW_GRDH_test.SAFE",
        })
        assert result["new_state"] == RunState.DONE

        # 5. Verify final state
        status = GetRunStatusUseCase(repo).execute(run_id)
        assert status["state"] == RunState.DONE
        assert "tc_tif" in status["artifacts"]
        assert "hand_tif" in status["artifacts"]
        assert "water_mask_tif" in status["artifacts"]
