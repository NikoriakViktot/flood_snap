"""The Flood Lab as a Celery worker of the platform (GEOPROC_TRANSPORT=celery in geoai-django).

The platform (Django) owns the job: it creates the Job row, sends `geoproc.basin_delineation(job_id, params)` to the
queue `geoproc` on the platform broker, and keeps the job state. This worker validates the request, runs the pipeline
in a memory-limited child process and reports every state change as the task `platform.geoproc_event(job_id,
payload)` on the platform queue — no HTTP callback, no callback token, no job files as the source of truth.
Contracts: contracts/basin_delineation.v1.json (task input) and contracts/job_event.v1.json (event payload); the
canonical copies live in geoai-django contracts/geoproc/.

  celery -A lab_service.tasks worker -Q geoproc -c 1 --prefetch-multiplier 1 -n lab@%h
"""
from __future__ import annotations

import logging

from celery import Celery

from lab_service import settings
from lab_service import validate as V
from lab_service.store import JobStore
from lab_service.terrain import TerrainStore

log = logging.getLogger("lab")

app = Celery("lab", broker=settings.BROKER_URL)
app.conf.update(
    task_default_queue=settings.CELERY_QUEUE,
    task_serializer="json", accept_content=["json"],
    task_acks_late=True,                 # a worker crash returns the job to the queue instead of losing it
    worker_prefetch_multiplier=1,        # one job at a time: the host has 7.8 GB for everything
    broker_transport_options={"visibility_timeout": 6 * 3600},
    worker_hijack_root_logger=False,
)

BASIN_TASK = "geoproc.basin_delineation"
EVENT_TASK = "platform.geoproc_event"


def emit(job_id: str, body: dict) -> None:
    """One state change of the job, to the platform (Django task platform.geoproc_event)."""
    app.send_task(EVENT_TASK, kwargs={"job_id": job_id, "payload": body}, queue=settings.PLATFORM_QUEUE)


@app.task(name=BASIN_TASK)
def basin_delineation(job_id: str, params: dict) -> str:
    from lab_service.worker import execute
    try:
        prepared = V.prepare(params, TerrainStore())
    except (V.Rejected, V.Unavailable) as e:
        emit(job_id, {"status": "failed", "progress_pct": 100, "stage": "rejected", "error": str(e),
                      "result_ref": None, "result": None, "products": []})
        return "failed"
    out_dir = JobStore().out_dir(job_id)
    return execute({"job_id": job_id, "params": prepared}, str(out_dir), lambda body: emit(job_id, body))["status"]
