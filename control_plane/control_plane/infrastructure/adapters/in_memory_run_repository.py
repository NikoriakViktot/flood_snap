"""Adapter: InMemoryRunRepository — dict-backed run persistence for testing."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from control_plane.domain.ports.run_repository import RunRecord, RunRepository


class InMemoryRunRepository(RunRepository):
    def __init__(self) -> None:
        self._store: dict[UUID, RunRecord] = {}

    def create(self, record: RunRecord) -> RunRecord:
        now = datetime.now(timezone.utc)
        record.created_at = record.created_at or now
        record.updated_at = record.updated_at or now
        self._store[record.id] = record
        return record

    def get(self, run_id: UUID) -> RunRecord | None:
        return self._store.get(run_id)

    def update_state(self, run_id: UUID, state: str) -> None:
        rec = self._store.get(run_id)
        if rec:
            rec.state = state
            rec.updated_at = datetime.now(timezone.utc)

    def update_artifacts(self, run_id: UUID, artifacts: dict) -> None:
        rec = self._store.get(run_id)
        if rec:
            rec.artifacts = {**rec.artifacts, **artifacts}
            rec.updated_at = datetime.now(timezone.utc)

    def list_runs(
        self,
        skip: int = 0,
        limit: int = 100,
        state: str | None = None,
        contract_type: str | None = None,
    ) -> list[RunRecord]:
        runs = list(self._store.values())
        if state:
            runs = [r for r in runs if r.state == state]
        if contract_type:
            runs = [r for r in runs if r.contract.get("type") == contract_type]
        runs.sort(key=lambda r: r.created_at or datetime.min, reverse=True)
        return runs[skip : skip + limit]

    def commit(self) -> None:
        pass  # no-op for in-memory
