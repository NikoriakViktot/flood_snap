"""Port: Clock — abstraction for current time."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime: ...
