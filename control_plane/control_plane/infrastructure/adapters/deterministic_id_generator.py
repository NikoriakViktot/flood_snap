"""Adapter: DeterministicIdGenerator — predictable IDs for testing."""

from control_plane.domain.ports.id_generator import IdGenerator


class DeterministicIdGenerator(IdGenerator):
    def __init__(self, prefix: str = "test") -> None:
        self._prefix = prefix
        self._counter = 0

    def generate(self) -> str:
        self._counter += 1
        return f"{self._prefix}-{self._counter:04d}"
