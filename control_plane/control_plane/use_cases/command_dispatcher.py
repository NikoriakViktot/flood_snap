"""Dispatch typed Command objects to the appropriate worker queue."""

from __future__ import annotations

import logging
from dataclasses import asdict

from control_plane.domain.commands import (
    Command, StartSar, StartDem, StartMask, StartAnalysis, Notify,
)
from control_plane.domain.ports.job_queue import JobQueue

log = logging.getLogger(__name__)

COMMAND_QUEUE_MAP = {
    StartSar: "flood_jobs",
    StartDem: "dem_jobs",
    StartMask: "mask_jobs",
    StartAnalysis: "dem_analysis_jobs",
}


def dispatch_command(queue: JobQueue, cmd: Command) -> None:
    """Push a typed command to the correct worker queue."""
    if isinstance(cmd, Notify):
        log.info("Pipeline DONE for run=%s, artifacts=%s", cmd.run_id, cmd.artifacts)
        return

    queue_name = COMMAND_QUEUE_MAP.get(type(cmd))
    if queue_name is None:
        log.warning("Unknown command type: %s", type(cmd).__name__)
        return

    payload = asdict(cmd)
    queue.push(queue_name, payload)
    log.info("Dispatched %s → %s (run=%s)", type(cmd).__name__, queue_name, cmd.run_id)
