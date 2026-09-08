"""
Dispatcher: pushes jobs to Redis queues for workers.

Actions:
  SAR_PREPROCESS → flood_jobs queue (SAR worker)
  DEM_COMPUTE    → dem_jobs queue (DEM worker)
  WATER_MASK     → mask_jobs queue (SAR worker, mask phase)
  NOTIFY         → logs (extensible: webhook, etc.)
"""

import json
import logging
import os
from uuid import UUID

import redis

log = logging.getLogger("dispatcher")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

_redis: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def dispatch(
    action: str,
    run_id: UUID,
    *,
    contract_payload: dict | None = None,
    artifacts: dict | None = None,
):
    """Dispatch an action by pushing a job to the appropriate Redis queue."""
    r = _get_redis()
    base = {
        "run_id": str(run_id),
        **(contract_payload or {}),
        **(artifacts or {}),
    }

    if action == "SAR_PREPROCESS":
        r.rpush("flood_jobs", json.dumps(base))
        log.info("Dispatched SAR_PREPROCESS → flood_jobs (run=%s)", run_id)
        return

    if action == "DEM_COMPUTE":
        r.rpush("dem_jobs", json.dumps(base))
        log.info("Dispatched DEM_COMPUTE → dem_jobs (run=%s)", run_id)
        return

    if action == "WATER_MASK":
        r.rpush("mask_jobs", json.dumps(base))
        log.info("Dispatched WATER_MASK → mask_jobs (run=%s)", run_id)
        return

    if action == "NOTIFY":
        log.info("Pipeline DONE for run=%s, artifacts=%s", run_id, artifacts)
        return

    log.warning("Unknown action: %s (run=%s)", action, run_id)
