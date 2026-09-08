"""Tests for in-memory adapters — verify they satisfy port contracts."""

import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "control_plane"))

try:
    from control_plane.domain.ports.run_repository import RunRecord
    from control_plane.infrastructure.adapters.in_memory_run_repository import InMemoryRunRepository
    from control_plane.infrastructure.adapters.in_memory_job_queue import InMemoryJobQueue
    from control_plane.infrastructure.adapters.in_memory_event_bus import InMemoryEventBus
    from control_plane.infrastructure.adapters.fake_clock import FakeClock
    from control_plane.infrastructure.adapters.deterministic_id_generator import DeterministicIdGenerator
    HAS_CP = True
except ImportError:
    HAS_CP = False

pytestmark = pytest.mark.skipif(not HAS_CP, reason="control_plane not available")


class TestInMemoryRunRepository:
    def test_create_and_get(self):
        repo = InMemoryRunRepository()
        rid = uuid4()
        rec = RunRecord(id=rid, state="CREATED", contract={"type": "test"}, artifacts={})
        repo.create(rec)
        got = repo.get(rid)
        assert got is not None
        assert got.state == "CREATED"

    def test_get_nonexistent(self):
        repo = InMemoryRunRepository()
        assert repo.get(uuid4()) is None

    def test_update_state(self):
        repo = InMemoryRunRepository()
        rid = uuid4()
        repo.create(RunRecord(id=rid, state="CREATED", contract={}, artifacts={}))
        repo.update_state(rid, "SAR_RUNNING")
        assert repo.get(rid).state == "SAR_RUNNING"

    def test_update_artifacts(self):
        repo = InMemoryRunRepository()
        rid = uuid4()
        repo.create(RunRecord(id=rid, state="CREATED", contract={}, artifacts={"a": 1}))
        repo.update_artifacts(rid, {"b": 2})
        rec = repo.get(rid)
        assert rec.artifacts == {"a": 1, "b": 2}

    def test_list_runs(self):
        repo = InMemoryRunRepository()
        for i in range(3):
            repo.create(RunRecord(id=uuid4(), state="CREATED", contract={"type": "t"}, artifacts={}))
        assert len(repo.list_runs()) == 3

    def test_list_runs_filter_state(self):
        repo = InMemoryRunRepository()
        r1 = uuid4()
        r2 = uuid4()
        repo.create(RunRecord(id=r1, state="CREATED", contract={}, artifacts={}))
        repo.create(RunRecord(id=r2, state="DONE", contract={}, artifacts={}))
        assert len(repo.list_runs(state="DONE")) == 1

    def test_list_runs_filter_contract_type(self):
        repo = InMemoryRunRepository()
        repo.create(RunRecord(id=uuid4(), state="CREATED", contract={"type": "a"}, artifacts={}))
        repo.create(RunRecord(id=uuid4(), state="CREATED", contract={"type": "b"}, artifacts={}))
        assert len(repo.list_runs(contract_type="a")) == 1

    def test_commit_noop(self):
        repo = InMemoryRunRepository()
        repo.commit()  # should not raise


class TestInMemoryJobQueue:
    def test_push_and_pop(self):
        q = InMemoryJobQueue()
        q.push("flood_jobs", {"run_id": "1"})
        result = q.pop(["flood_jobs"])
        assert result == ("flood_jobs", {"run_id": "1"})

    def test_pop_empty_returns_none(self):
        q = InMemoryJobQueue()
        assert q.pop(["flood_jobs"]) is None

    def test_pop_priority(self):
        q = InMemoryJobQueue()
        q.push("dem_jobs", {"x": 1})
        q.push("flood_jobs", {"x": 2})
        name, payload = q.pop(["flood_jobs", "dem_jobs"])
        assert name == "flood_jobs"

    def test_peek(self):
        q = InMemoryJobQueue()
        q.push("q", {"a": 1})
        q.push("q", {"b": 2})
        assert len(q.peek("q")) == 2

    def test_size(self):
        q = InMemoryJobQueue()
        q.push("q", {})
        q.push("q", {})
        assert q.size("q") == 2


class TestInMemoryEventBus:
    def test_emit_and_consume(self):
        bus = InMemoryEventBus()
        bus.emit({"event": "SAR_COMPLETED"})
        got = bus.consume()
        assert got == {"event": "SAR_COMPLETED"}

    def test_consume_empty_returns_none(self):
        bus = InMemoryEventBus()
        assert bus.consume() is None


class TestFakeClock:
    def test_fixed_time(self):
        from datetime import datetime, timezone
        fixed = datetime(2025, 6, 1, tzinfo=timezone.utc)
        clock = FakeClock(fixed)
        assert clock.now() == fixed

    def test_advance(self):
        clock = FakeClock()
        t1 = clock.now()
        clock.advance(hours=1)
        t2 = clock.now()
        assert (t2 - t1).total_seconds() == 3600


class TestDeterministicIdGenerator:
    def test_sequential(self):
        gen = DeterministicIdGenerator("run")
        assert gen.generate() == "run-0001"
        assert gen.generate() == "run-0002"
