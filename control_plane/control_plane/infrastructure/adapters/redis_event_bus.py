"""Adapter: RedisEventBus — Redis-backed event bus on cp_events queue."""

from __future__ import annotations

import json
import logging
from typing import Any

import redis as redis_lib

from control_plane.domain.ports.event_bus import EventBus

log = logging.getLogger(__name__)


class RedisEventBus(EventBus):
    QUEUE = "cp_events"

    def __init__(self, client: redis_lib.Redis) -> None:
        self._r = client

    def emit(self, event: dict[str, Any]) -> None:
        self._r.rpush(self.QUEUE, json.dumps(event))
        log.info("Emitted event to %s", self.QUEUE)

    def consume(self, timeout: int = 5) -> dict[str, Any] | None:
        result = self._r.blpop(self.QUEUE, timeout=timeout)
        if result is None:
            return None
        return json.loads(result[1])
