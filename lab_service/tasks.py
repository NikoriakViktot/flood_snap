"""The Flood Lab as a Celery worker of the platform (GEOPROC_TRANSPORT=celery in geoai-django).

The platform (Django) owns the job: it creates the Job row, sends `geoproc.basin_delineation(job_id, params)` to the
queue `geoproc` on the platform broker, and keeps the job state. This worker validates the request, runs the pipeline
in a memory-limited child process and reports every state change as the task `platform.geoproc_event(job_id,
payload)` on the platform queue — no HTTP callback, no callback token, no job files as the source of truth.
Contracts: contracts/basin_delineation.v1.json, contracts/hand_model.v1.json and contracts/gr4h_run.v1.json (task inputs), contracts/job_event.v1.json
(event payload); the canonical copies live in geoai-django contracts/geoproc/.

  celery -A lab_service.tasks worker -Q geoproc -P solo --prefetch-multiplier 1 -n lab@%h

`-P solo`: the task runs in the worker's main process, which then starts the memory-limited pipeline child itself
(the default prefork pool runs tasks in daemonic processes, which may not have children).
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
HAND_TASK = "geoproc.hand_model"
GR4H_TASK = "geoproc.gr4h_run"
EVENT_TASK = "platform.geoproc_event"


def emit(job_id: str, body: dict) -> None:
    """One state change of the job, to the platform (Django task platform.geoproc_event)."""
    app.send_task(EVENT_TASK, kwargs={"job_id": job_id, "payload": body}, queue=settings.PLATFORM_QUEUE)


def memory_gate(task, job_id: str) -> bool:
    """True when the job may start. Short of memory: retry later (raises celery Retry), or after the last retry report
    the job failed with a plain message and return False."""
    from lab_service.memguard import BUSY_MESSAGE, short_of_memory
    short, free = short_of_memory()
    if not short:
        return True
    if task.request.retries < settings.MEM_RETRIES:
        log.warning("job %s waits: MemAvailable %s MiB < %s MiB (retry %d/%d)", job_id, free, settings.MIN_FREE_MEM_MB,
                    task.request.retries + 1, settings.MEM_RETRIES)
        raise task.retry(countdown=settings.MEM_RETRY_S, max_retries=settings.MEM_RETRIES)
    emit(job_id, {"status": "failed", "progress_pct": 100, "stage": "server_busy",
                  "error": f"{BUSY_MESSAGE} (free {free} MiB, needs {settings.MIN_FREE_MEM_MB} MiB)",
                  "result_ref": None, "result": None, "products": []})
    return False


@app.task(name=BASIN_TASK, bind=True)
def basin_delineation(self, job_id: str, params: dict) -> str:
    from lab_service.worker import execute
    if not memory_gate(self, job_id):
        return "failed"
    try:
        prepared = V.prepare(params, TerrainStore())
    except (V.Rejected, V.Unavailable) as e:
        emit(job_id, {"status": "failed", "progress_pct": 100, "stage": "rejected", "error": str(e),
                      "result_ref": None, "result": None, "products": []})
        return "failed"
    out_dir = JobStore().out_dir(job_id)
    try:
        return execute({"job_id": job_id, "params": prepared}, str(out_dir), lambda body: emit(job_id, body))["status"]
    except Exception as e:  # noqa: BLE001  never leave the platform's job 'running' on an unexpected error
        log.exception("job %s crashed", job_id)
        emit(job_id, {"status": "failed", "progress_pct": 100, "stage": "crashed", "error": f"{type(e).__name__}: {e}",
                      "result_ref": None, "result": None, "products": []})
        return "failed"


@app.task(name=HAND_TASK, bind=True)
def hand_model(self, job_id: str, params: dict) -> str:
    """HAND model of one delineated basin (contracts/hand_model.v1.json): the delineation's transport, the pipeline
    child is lab_service.hand.runner.hand_child."""
    from lab_service.hand import runner
    from lab_service.hand import validate as HV
    from lab_service.worker import execute
    if not memory_gate(self, job_id):
        return "failed"
    try:
        prepared = HV.prepare_hand(params, TerrainStore())
    except (V.Rejected, V.Unavailable) as e:
        emit(job_id, {"status": "failed", "progress_pct": 100, "stage": "rejected", "error": str(e),
                      "result_ref": None, "result": None, "products": []})
        return "failed"
    out_dir = JobStore().out_dir(job_id)
    try:
        return execute({"job_id": job_id, "kind": "hand_model", "params": prepared}, str(out_dir),
                       lambda body: emit(job_id, body), target=runner.hand_child)["status"]
    except Exception as e:  # noqa: BLE001  never leave the platform's job 'running' on an unexpected error
        log.exception("job %s crashed", job_id)
        emit(job_id, {"status": "failed", "progress_pct": 100, "stage": "crashed", "error": f"{type(e).__name__}: {e}",
                      "result_ref": None, "result": None, "products": []})
        return "failed"


@app.task(name=GR4H_TASK, bind=True)
def gr4h_run(self, job_id: str, params: dict) -> str:
    """CemaNeige-GR4J (daily, default) or CemaNeige-GR4H (hourly) on one delineated basin (contracts/gr4h_run.v1.json):
    the hand_model's transport, the pipeline child is lab_service.gr4h.runner.gr4h_child (airGR as a short-lived
    Rscript inside the memory-limited child)."""
    from lab_service.gr4h import runner
    from lab_service.gr4h import validate as GV
    from lab_service.worker import execute
    if not memory_gate(self, job_id):
        return "failed"
    try:
        prepared = GV.prepare_gr4h(params)
    except (V.Rejected, V.Unavailable) as e:
        emit(job_id, {"status": "failed", "progress_pct": 100, "stage": "rejected", "error": str(e),
                      "result_ref": None, "result": None, "products": []})
        return "failed"
    out_dir = JobStore().out_dir(job_id)
    try:
        return execute({"job_id": job_id, "kind": "gr4h_run", "params": prepared}, str(out_dir),
                       lambda body: emit(job_id, body), target=runner.gr4h_child)["status"]
    except Exception as e:  # noqa: BLE001  never leave the platform's job 'running' on an unexpected error
        log.exception("job %s crashed", job_id)
        emit(job_id, {"status": "failed", "progress_pct": 100, "stage": "crashed", "error": f"{type(e).__name__}: {e}",
                      "result_ref": None, "result": None, "products": []})
        return "failed"
