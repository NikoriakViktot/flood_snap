"""The memory guard of the Lab worker: MemAvailable read from /proc/meminfo; a job waits (Celery retry) while the host
is short of memory and fails with a plain message after the last retry."""
from __future__ import annotations

import pytest
from celery.exceptions import Retry

from lab_service import memguard, settings, tasks

MEMINFO = "MemTotal:        7999012 kB\nMemFree:          300000 kB\nMemAvailable:    {} kB\nBuffers:           1 kB\n"


def _meminfo(tmp_path, kb):
    p = tmp_path / "meminfo"
    p.write_text(MEMINFO.format(kb))
    return str(p)


def test_reads_mem_available(tmp_path):
    assert memguard.mem_available_mb(_meminfo(tmp_path, 5_000_000)) == 4882
    assert memguard.mem_available_mb(str(tmp_path / "missing")) is None
    assert memguard.short_of_memory(_meminfo(tmp_path, 1_000_000)) == (True, 976)
    assert memguard.short_of_memory(_meminfo(tmp_path, 3_000_000))[0] is False


class _Req:
    def __init__(self, retries):
        self.retries = retries


class _Task:
    def __init__(self, retries):
        self.request = _Req(retries)

    def retry(self, countdown, max_retries):
        assert countdown == settings.MEM_RETRY_S and max_retries == settings.MEM_RETRIES
        return Retry("later")


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(tasks.app, "send_task", lambda name, kwargs, queue: out.append(kwargs))
    return out


def test_gate_waits_then_fails_with_a_plain_message(monkeypatch, sent):
    monkeypatch.setattr(memguard, "short_of_memory", lambda: (True, 900))
    with pytest.raises(Retry):
        tasks.memory_gate(_Task(0), "j1")
    assert sent == []
    assert tasks.memory_gate(_Task(settings.MEM_RETRIES), "j1") is False
    body = sent[-1]["payload"]
    assert body["status"] == "failed" and body["stage"] == "server_busy" and "server stayed busy" in body["error"]


def test_gate_lets_jobs_run_when_memory_is_free(monkeypatch, sent):
    monkeypatch.setattr(memguard, "short_of_memory", lambda: (False, 4000))
    assert tasks.memory_gate(_Task(0), "j1") is True and sent == []
