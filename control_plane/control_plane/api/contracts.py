"""POST /contracts — submit a flood/hydro processing contract."""

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from control_plane.infrastructure.db.session import get_db
from control_plane.infrastructure.adapters.postgres_run_repository import PostgresRunRepository
from control_plane.infrastructure.adapters.redis_job_queue import RedisJobQueue
from control_plane.infrastructure.dispatcher import _get_redis
from control_plane.use_cases.submit_contract import SubmitContractUseCase

router = APIRouter()


@router.post("/contracts")
def submit_contract(payload: Dict[str, Any], db: Session = Depends(get_db)):
    repo = PostgresRunRepository(db)
    queue = RedisJobQueue(_get_redis())
    uc = SubmitContractUseCase(repo, queue)

    try:
        return uc.execute(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
