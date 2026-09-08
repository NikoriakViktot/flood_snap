"""Port: RunRepository — persistence abstraction for pipeline runs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass
class RunRecord:
    id: UUID
    state: str
    contract: dict[str, Any]
    artifacts: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RunRepository(ABC):
    @abstractmethod
    def create(self, record: RunRecord) -> RunRecord: ...

    @abstractmethod
    def get(self, run_id: UUID) -> RunRecord | None: ...

    @abstractmethod
    def update_state(self, run_id: UUID, state: str) -> None: ...

    @abstractmethod
    def update_artifacts(self, run_id: UUID, artifacts: dict[str, Any]) -> None: ...

    @abstractmethod
    def list_runs(
        self,
        skip: int = 0,
        limit: int = 100,
        state: str | None = None,
        contract_type: str | None = None,
    ) -> list[RunRecord]: ...

    @abstractmethod
    def commit(self) -> None: ...
