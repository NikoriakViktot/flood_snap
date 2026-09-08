"""Adapter: InMemoryEventBus — deque-backed event bus for testing."""

from __future__ import annotations

from collections import deque
from typing import Any

from control_plane.domain.ports.event_bus import EventBus


class InMemoryEventBus(EventBus):
    def __init__(self) -> None:
        self._queue: deque[dict[str, Any]] = deque()

    def emit(self, event: dict[str, Any]) -> None:
        self._queue.append(event)

    def consume(self, timeout: int = 5) -> dict[str, Any] | None:
        if self._queue:
            return self._queue.popleft()
        return None
