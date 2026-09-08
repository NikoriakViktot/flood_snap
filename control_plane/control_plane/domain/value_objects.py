"""Domain value objects — validated, immutable wrappers around primitive types."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class RunId:
    value: UUID

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True)
class SceneId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.endswith(".SAFE"):
            raise ValueError(f"Scene ID must end with .SAFE, got: {self.value!r}")


@dataclass(frozen=True)
class BasinHash:
    value: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{12}", self.value):
            raise ValueError(f"BasinHash must be 12 hex chars, got: {self.value!r}")


@dataclass(frozen=True)
class ArtifactURI:
    value: str

    def __post_init__(self) -> None:
        if not self.value.startswith("/"):
            raise ValueError(f"ArtifactURI must be absolute path, got: {self.value!r}")


@dataclass(frozen=True)
class ContractHash:
    """SHA256 hash for contract idempotency."""
    value: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.value):
            raise ValueError(f"ContractHash must be 64 hex chars, got: {self.value!r}")


@dataclass(frozen=True)
class StepName:
    value: str

    VALID = {"SAR", "DEM", "MASK"}

    def __post_init__(self) -> None:
        if self.value not in self.VALID:
            raise ValueError(f"StepName must be one of {self.VALID}, got: {self.value!r}")


@dataclass(frozen=True)
class ArtifactType:
    value: str

    VALID = {"sigma0", "hand", "slope", "mask", "tc", "elevation"}

    def __post_init__(self) -> None:
        if self.value not in self.VALID:
            raise ValueError(f"ArtifactType must be one of {self.VALID}, got: {self.value!r}")
