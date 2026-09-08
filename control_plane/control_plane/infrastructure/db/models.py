import uuid
from sqlalchemy import Column, Text, DateTime
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func

from control_plane.infrastructure.db.base import Base


class Run(Base):
    __tablename__ = "runs"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    state = Column(Text, nullable=False)
    contract = Column(JSONB, nullable=False)
    artifacts = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
