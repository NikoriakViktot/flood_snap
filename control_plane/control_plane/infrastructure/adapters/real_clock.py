"""Adapter: RealClock — system clock."""

from datetime import datetime, timezone

from control_plane.domain.ports.clock import Clock


class RealClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)
