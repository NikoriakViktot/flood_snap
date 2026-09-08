"""Use case: Handle a worker event — load run, transition, dispatch next command."""

from __future__ import annotations

import logging
from uuid import UUID

from control_plane.domain.states import RunState
from control_plane.domain.events import EventType
from control_plane.domain.policies.run_state_machine import transition, build_command
from control_plane.domain.ports.run_repository import RunRepository
from control_plane.domain.ports.job_queue import JobQueue
from control_plane.use_cases.command_dispatcher import dispatch_command

log = logging.getLogger(__name__)


class HandleEventUseCase:
    def __init__(self, repo: RunRepository, queue: JobQueue) -> None:
        self._repo = repo
        self._queue = queue

    def execute(self, run_id: str | UUID, event: str | EventType, artifacts: dict | None = None) -> dict:
        run_uuid = UUID(str(run_id))
        event_type = EventType(event) if isinstance(event, str) else event

        # 1. Load run
        record = self._repo.get(run_uuid)
        if record is None:
            raise LookupError(f"Run {run_id} does not exist.")

        current_state = RunState(record.state)

        # 2. Merge artifacts
        if artifacts:
            self._repo.update_artifacts(run_uuid, artifacts)
            record.artifacts = {**record.artifacts, **artifacts}

        # 3. Transition
        new_state = transition(current_state, event_type)
        self._repo.update_state(run_uuid, new_state)
        record.state = new_state
        self._repo.commit()

        # 4. Build and dispatch command
        cmd = build_command(new_state, event_type, record)
        action_name = None
        if cmd:
            dispatch_command(self._queue, cmd)
            action_name = type(cmd).__name__

        log.info("Event %s: run=%s %s→%s action=%s",
                 event_type, run_id, current_state, new_state, action_name)

        return {
            "ok": True,
            "run_id": str(run_uuid),
            "previous_state": current_state,
            "new_state": new_state,
            "action_dispatched": action_name,
        }
