"""
Contract envelope — generic wrapper for all V1 contracts.
"""

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class ContractEnvelopeV1(BaseModel):
    """Strict envelope for all V1 contracts."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str = Field(..., description="Contract type (e.g. flood.pipeline.v1)")
    spec_version: Literal["v1"] = Field("v1")
    idempotency_key: str | None = Field(None, description="Optional dedup key")
    payload: dict[str, Any] = Field(..., description="Contract-specific payload")
