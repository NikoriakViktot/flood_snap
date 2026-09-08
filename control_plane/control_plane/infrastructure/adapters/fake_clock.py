"""Adapter: FakeClock — deterministic clock for testing."""

from datetime import datetime, timezone

from control_plane.domain.ports.clock import Clock


class FakeClock(Clock):
    def __init__(self, fixed: datetime | None = None) -> None:
        self._now = fixed or datetime(2025, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        """Advance clock by timedelta kwargs (hours=1, seconds=30, etc.)."""
        from datetime import timedelta
        self._now += timedelta(**kwargs)
