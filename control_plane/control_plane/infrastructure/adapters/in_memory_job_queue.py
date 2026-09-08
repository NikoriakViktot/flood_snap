"""Adapter: InMemoryJobQueue — deque-backed job queue for testing."""

from __future__ import annotations

from collections import deque
from typing import Any

from control_plane.domain.ports.job_queue import JobQueue


class InMemoryJobQueue(JobQueue):
    def __init__(self) -> None:
        self._queues: dict[str, deque[dict[str, Any]]] = {}

    def push(self, queue_name: str, payload: dict[str, Any]) -> None:
        if queue_name not in self._queues:
            self._queues[queue_name] = deque()
        self._queues[queue_name].append(payload)

    def pop(self, queue_names: list[str], timeout: int = 5) -> tuple[str, dict[str, Any]] | None:
        for name in queue_names:
            q = self._queues.get(name)
            if q:
                return name, q.popleft()
        return None

    def peek(self, queue_name: str) -> list[dict[str, Any]]:
        """Test helper: see queue contents without consuming."""
        return list(self._queues.get(queue_name, []))

    def size(self, queue_name: str) -> int:
        """Test helper: get queue length."""
        return len(self._queues.get(queue_name, []))
