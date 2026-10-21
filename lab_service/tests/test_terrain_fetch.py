"""TerrainStore._fetch: parallel refreshes of one key, stale copy on a failed refresh (AWS, 2026-10-21)."""
from __future__ import annotations

import os
import threading
import time

import pytest

from lab_service import settings
from lab_service.terrain import TerrainStore


def test_parallel_refresh_of_one_key_does_not_fail(tmp_path, monkeypatch):
    src = tmp_path / "src"
    (src / "_districts").mkdir(parents=True)
    (src / "_districts" / "a.geojson").write_text("x" * 200_000)
    monkeypatch.setattr(settings, "TERRAIN_LOCAL", str(src))
    t = TerrainStore(tmp_path / "cache")
    dst = t.cache / "_districts" / "a.geojson"
    errors = []

    def go():
        try:
            t._fetch("_districts/a.geojson", dst, max_age_s=0)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=go) for _ in range(16)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert not errors and dst.read_text() == "x" * 200_000
    assert not [p for p in dst.parent.iterdir() if p.name.endswith(".part")]


def test_failed_refresh_serves_the_stale_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "TERRAIN_LOCAL", str(tmp_path / "missing"))
    t = TerrainStore(tmp_path / "cache")
    dst = t.cache / "_districts" / "a.geojson"
    dst.parent.mkdir(parents=True)
    dst.write_text("old")
    old = time.time() - 3 * 86400
    os.utime(dst, (old, old))
    assert t._fetch("_districts/a.geojson", dst, max_age_s=86400).read_text() == "old"
    dst.unlink()
    with pytest.raises(FileNotFoundError):
        t._fetch("_districts/a.geojson", dst, max_age_s=86400)
