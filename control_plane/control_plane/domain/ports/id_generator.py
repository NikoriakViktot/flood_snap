"""Port: IdGenerator — abstraction for unique ID generation."""

from __future__ import annotations

from abc import ABC, abstractmethod


class IdGenerator(ABC):
    @abstractmethod
    def generate(self) -> str: ...
