"""Adapter: UuidIdGenerator — UUID4-based ID generation."""

import uuid

from control_plane.domain.ports.id_generator import IdGenerator


class UuidIdGenerator(IdGenerator):
    def generate(self) -> str:
        return str(uuid.uuid4())
