"""Memory guard of the Lab worker (owner, 2026-10-17): the host is small (2 vCPU, ~8 GB) and shared with the platform,
Hydro, the tile servers and Postgres, and the containers' memory limits add up to more than the host has, so they do
not protect it. Before a job starts the worker reads the host's MemAvailable; below MIN_FREE_MEM_MB the job stays
queued (Celery retry every MEM_RETRY_S, MEM_RETRIES times: 20 min by default — the platform shows "the server is busy,
the calculation is queued" meanwhile) and then fails with a plain message instead of pushing the host into swap or the
OOM killer. Inside Docker /proc/meminfo is the host's (no lxcfs on the server)."""
from __future__ import annotations

from lab_service import settings

BUSY_MESSAGE = ("the server stayed busy (short of memory) for the whole waiting time, so the calculation was not "
                "started — start it again later")


def mem_available_mb(path: str = "/proc/meminfo") -> int | None:
    """MemAvailable in MiB, or None when it cannot be read (then the guard lets the job run)."""
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def short_of_memory(path: str = "/proc/meminfo") -> tuple[bool, int | None]:
    free = mem_available_mb(path)
    return (free is not None and free < settings.MIN_FREE_MEM_MB), free
