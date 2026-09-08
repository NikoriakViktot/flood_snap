"""Redis utilities — shared between workers.

Canonical implementations of connect_redis and emit_event.
"""

import json
import logging
import os
import time

import redis

log = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")


def connect_redis(url: str | None = None, retries: int = 30) -> redis.Redis:
    """Connect to Redis with retry loop."""
    url = url or REDIS_URL
    for attempt in range(retries):
        try:
            r = redis.Redis.from_url(url, decode_responses=True)
            r.ping()
            log.info("Connected to Redis")
            return r
        except redis.ConnectionError:
            log.warning("Redis not ready, retry %d/%d …", attempt + 1, retries)
            time.sleep(2)
    raise RuntimeError("Cannot connect to Redis")


def emit_event(
    r: redis.Redis,
    run_id: str | None,
    event: str,
    artifacts: dict,
    queue: str = "cp_events",
) -> None:
    """Push event to control plane events queue."""
    if not run_id:
        return
    r.rpush(queue, json.dumps({
        "run_id": run_id,
        "event": event,
        "artifacts": artifacts,
    }))
