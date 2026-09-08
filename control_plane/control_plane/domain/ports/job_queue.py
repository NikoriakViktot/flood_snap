"""Port: JobQueue — abstraction for dispatching worker jobs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class JobQueue(ABC):
    @abstractmethod
    def push(self, queue_name: str, payload: dict[str, Any]) -> None: ...

    @abstractmethod
    def pop(self, queue_names: list[str], timeout: int = 5) -> tuple[str, dict[str, Any]] | None: ...
