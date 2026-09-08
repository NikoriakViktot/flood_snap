"""Adapter: PostgresRunRepository — SQLAlchemy-backed run persistence."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from control_plane.domain.ports.run_repository import RunRecord, RunRepository
from control_plane.infrastructure.db.models import Run


class PostgresRunRepository(RunRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def _to_record(self, run: Run) -> RunRecord:
        return RunRecord(
            id=run.id,
            state=run.state,
            contract=run.contract,
            artifacts=run.artifacts or {},
            created_at=run.created_at,
            updated_at=run.updated_at,
        )

    def create(self, record: RunRecord) -> RunRecord:
        run = Run(
            id=record.id,
            state=record.state,
            contract=record.contract,
            artifacts=record.artifacts,
        )
        self._session.add(run)
        self._session.flush()
        self._session.refresh(run)
        return self._to_record(run)

    def get(self, run_id: UUID) -> RunRecord | None:
        run = self._session.query(Run).filter(Run.id == run_id).first()
        if run is None:
            return None
        return self._to_record(run)

    def update_state(self, run_id: UUID, state: str) -> None:
        run = self._session.query(Run).filter(Run.id == run_id).first()
        if run:
            run.state = state

    def update_artifacts(self, run_id: UUID, artifacts: dict) -> None:
        run = self._session.query(Run).filter(Run.id == run_id).first()
        if run:
            run.artifacts = {**(run.artifacts or {}), **artifacts}

    def list_runs(
        self,
        skip: int = 0,
        limit: int = 100,
        state: str | None = None,
        contract_type: str | None = None,
    ) -> list[RunRecord]:
        q = self._session.query(Run)
        if state:
            q = q.filter(Run.state == state)
        if contract_type:
            q = q.filter(Run.contract["type"].astext == contract_type)
        runs = q.order_by(Run.created_at.desc()).offset(skip).limit(limit).all()
        return [self._to_record(r) for r in runs]

    def commit(self) -> None:
        self._session.commit()
