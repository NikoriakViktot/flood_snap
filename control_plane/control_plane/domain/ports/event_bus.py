"""Port: EventBus — abstraction for domain event transport."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class EventBus(ABC):
    @abstractmethod
    def emit(self, event: dict[str, Any]) -> None: ...

    @abstractmethod
    def consume(self, timeout: int = 5) -> dict[str, Any] | None: ...
