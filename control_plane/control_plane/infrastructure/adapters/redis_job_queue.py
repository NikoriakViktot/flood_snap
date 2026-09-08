"""Adapter: RedisJobQueue — Redis-backed job queue."""

from __future__ import annotations

import json
import logging
from typing import Any

import redis as redis_lib

from control_plane.domain.ports.job_queue import JobQueue

log = logging.getLogger(__name__)


class RedisJobQueue(JobQueue):
    def __init__(self, client: redis_lib.Redis) -> None:
        self._r = client

    def push(self, queue_name: str, payload: dict[str, Any]) -> None:
        self._r.rpush(queue_name, json.dumps(payload))
        log.info("Pushed job to %s", queue_name)

    def pop(self, queue_names: list[str], timeout: int = 5) -> tuple[str, dict[str, Any]] | None:
        result = self._r.blpop(queue_names, timeout=timeout)
        if result is None:
            return None
        queue, data = result
        if isinstance(queue, bytes):
            queue = queue.decode()
        return queue, json.loads(data)
