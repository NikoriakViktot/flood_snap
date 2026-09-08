"""GET /runs — list and filter pipeline runs."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from control_plane.infrastructure.db.session import get_db
from control_plane.infrastructure.adapters.postgres_run_repository import PostgresRunRepository
from control_plane.use_cases.get_run_status import GetRunStatusUseCase

router = APIRouter()


@router.get("/runs/{run_id}")
def get_run(run_id: UUID, db: Session = Depends(get_db)):
    uc = GetRunStatusUseCase(PostgresRunRepository(db))
    result = uc.execute(run_id)
    if not result:
        raise HTTPException(status_code=404, detail="Run not found")
    return result


@router.get("/runs")
def list_runs(
    skip: int = 0,
    limit: int = 100,
    status: Optional[str] = Query(None),
    contract_type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    uc = GetRunStatusUseCase(PostgresRunRepository(db))
    return uc.list(skip=skip, limit=limit, state=status, contract_type=contract_type)
