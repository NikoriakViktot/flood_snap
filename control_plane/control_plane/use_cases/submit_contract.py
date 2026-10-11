"""Use case: Submit a processing contract — validate, create run, dispatch first command."""

from __future__ import annotations

import logging
from uuid import uuid4

from control_plane.domain.contracts.validate import validate_contract
from control_plane.domain.states import RunState
from control_plane.domain.events import EventType
from control_plane.domain.policies.run_state_machine import transition, build_command
from control_plane.domain.ports.run_repository import RunRecord, RunRepository
from control_plane.domain.ports.job_queue import JobQueue
from control_plane.use_cases.command_dispatcher import dispatch_command

log = logging.getLogger(__name__)


class SubmitContractUseCase:
    def __init__(self, repo: RunRepository, queue: JobQueue) -> None:
        self._repo = repo
        self._queue = queue

    def execute(self, payload: dict) -> dict:
        # 1. Validate
        c_type, c_ver, c_model = validate_contract(payload)

        # 2. Create Run
        record = RunRecord(
            id=uuid4(),
            state=RunState.CREATED,
            contract={
                "type": c_type,
                "spec_version": c_ver,
                "payload": c_model.model_dump(mode="json"),
            },
            artifacts={},
        )
        record = self._repo.create(record)

        # 3. Determine initial state based on contract type
        if c_type == "dem.hand.v1":
            new_state = RunState.DEM_RUNNING
        elif c_type == "flood.mask.v1":
            new_state = RunState.MASK_RUNNING
        elif c_type == "dem.analysis.v1":
            new_state = RunState.ANALYSIS_RUNNING
        elif c_type == "basin.delineation.v1":
            new_state = RunState.BASIN_RUNNING
        else:
            new_state = transition(RunState.CREATED, EventType.CONTRACT_ACCEPTED)

        self._repo.update_state(record.id, new_state)
        record.state = new_state
        self._repo.commit()

        # 4. Build and dispatch command
        # For standalone contracts, we need a matching event
        if c_type == "dem.hand.v1":
            event = EventType.SAR_COMPLETED
        elif c_type == "flood.mask.v1":
            event = EventType.DEM_COMPLETED
        elif c_type == "dem.analysis.v1":
            event = EventType.CONTRACT_ACCEPTED
        else:
            event = EventType.CONTRACT_ACCEPTED

        cmd = build_command(new_state, event, record)
        action_name = None
        if cmd:
            dispatch_command(self._queue, cmd)
            action_name = type(cmd).__name__

        return {
            "run_id": str(record.id),
            "state": new_state,
            "dispatched_action": action_name,
        }
