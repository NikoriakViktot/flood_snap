"""Use case: Get run status — query run(s) from repository."""

from __future__ import annotations

from uuid import UUID

from control_plane.domain.ports.run_repository import RunRecord, RunRepository


class GetRunStatusUseCase:
    def __init__(self, repo: RunRepository) -> None:
        self._repo = repo

    def _to_dict(self, rec: RunRecord) -> dict:
        return {
            "id": str(rec.id),
            "state": rec.state,
            "contract": rec.contract,
            "artifacts": rec.artifacts,
            "created_at": rec.created_at.isoformat() if rec.created_at else None,
            "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
        }

    def execute(self, run_id: str | UUID) -> dict | None:
        record = self._repo.get(UUID(str(run_id)))
        if record is None:
            return None
        return self._to_dict(record)

    def list(
        self,
        skip: int = 0,
        limit: int = 100,
        state: str | None = None,
        contract_type: str | None = None,
    ) -> list[dict]:
        records = self._repo.list_runs(skip=skip, limit=limit, state=state, contract_type=contract_type)
        return [self._to_dict(r) for r in records]
