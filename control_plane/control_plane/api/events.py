"""POST /events — handle worker callbacks (state transitions)."""

from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from control_plane.domain.events import EventType
from control_plane.infrastructure.db.session import get_db
from control_plane.infrastructure.adapters.postgres_run_repository import PostgresRunRepository
from control_plane.infrastructure.adapters.redis_job_queue import RedisJobQueue
from control_plane.infrastructure.dispatcher import _get_redis
from control_plane.use_cases.handle_event import HandleEventUseCase

router = APIRouter()


class EventEnvelope(BaseModel):
    run_id: UUID
    event: EventType
    artifacts: Dict[str, Any] = Field(default_factory=dict)


@router.post("/events")
def handle_event(body: EventEnvelope, db: Session = Depends(get_db)):
    repo = PostgresRunRepository(db)
    queue = RedisJobQueue(_get_redis())
    uc = HandleEventUseCase(repo, queue)

    try:
        return uc.execute(body.run_id, body.event, body.artifacts)
    except LookupError:
        raise HTTPException(
            status_code=409,
            detail=f"Run {body.run_id} does not exist.",
        )
