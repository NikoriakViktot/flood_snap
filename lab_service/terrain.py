"""The precomputed national terrain (scripts/national/stage_terrain.py) read from S3 through a disk cache.

Layout: <prefix>/_districts/{index.json,<slug>.geojson}, <prefix>/dem_source=<dem>/manifest.json,
<prefix>/dem_source=<dem>/<slug>/{d8,streams,hand,dem}.tif. A district raster is downloaded on first use and kept
under CACHE_ROOT; its sha256 from the manifest is checked once after the download. TERRAIN_LOCAL points at a local
copy of the same tree (tests, the workstation) and bypasses S3.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.prepared import prep

from lab_service import settings

FILES = ("d8", "streams", "hand", "dem")


@dataclass(frozen=True)
class District:
    code: str
    slug: str
    name_ua: str
    km2: float


class TerrainStore:
    def __init__(self, cache_root: Path | None = None):
        self.cache = Path(cache_root or settings.CACHE_ROOT)
        self.cache.mkdir(parents=True, exist_ok=True)
        self._districts: list[tuple[District, object]] | None = None
        self._manifests: dict[str, dict] = {}
        self._s3 = None

    # ---- fetching ------------------------------------------------------------------------------
    def _fetch(self, key: str, dst: Path, max_age_s: float | None = None) -> Path:
        """key relative to the terrain prefix → local file (cached)."""
        if dst.exists() and (max_age_s is None or time.time() - dst.stat().st_mtime < max_age_s):
            return dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".part")
        if settings.TERRAIN_LOCAL:
            shutil.copyfile(Path(settings.TERRAIN_LOCAL) / key, tmp)
        else:
            if self._s3 is None:
                import boto3
                self._s3 = boto3.client("s3")
            self._s3.download_file(settings.S3_BUCKET, f"{settings.TERRAIN_PREFIX}/{key}", str(tmp))
        os.replace(tmp, dst)
        return dst

    def _json(self, key: str, max_age_s: float = 3600) -> dict:
        return json.loads(self._fetch(key, self.cache / key, max_age_s).read_text())

    # ---- districts -----------------------------------------------------------------------------
    def districts(self) -> list[District]:
        return [d for d, _ in self._load_districts()]

    def _load_districts(self):
        if self._districts is None:
            index = self._json("_districts/index.json")
            out = []
            for d in index:
                g = json.loads(self._fetch(f"_districts/{d['slug']}.geojson", self.cache / "_districts" / f"{d['slug']}.geojson", 86400).read_text())
                geom = shape(g["features"][0]["geometry"] if g.get("type") == "FeatureCollection" else g.get("geometry", g))
                out.append((District(d["code"], d["slug"], d["name_ua"], float(d["km2"])), prep(geom)))
            self._districts = out
        return self._districts

    def district_by_code(self, code: str) -> District | None:
        return next((d for d in self.districts() if d.code == code or d.slug == code), None)

    def find_district(self, lon: float, lat: float) -> District | None:
        p = Point(lon, lat)
        for d, g in self._load_districts():
            if g.contains(p):
                return d
        return None

    # ---- rasters -------------------------------------------------------------------------------
    def manifest(self, dem: str) -> dict:
        if dem not in self._manifests:
            self._manifests[dem] = self._json(f"dem_source={dem}/manifest.json")
        return self._manifests[dem]

    def available(self, dem: str) -> bool:
        try:
            self.manifest(dem)
            return True
        except Exception:
            return False

    def ensure(self, dem: str, slug: str, name: str) -> Path:
        """Local path of one district raster, downloaded on first use and verified against the manifest."""
        entry = next(d for d in self.manifest(dem)["districts"] if d["slug"] == slug)
        meta = entry["files"][f"{name}.tif"]
        dst = self.cache / f"dem_source={dem}" / slug / f"{name}.tif"
        if dst.exists() and dst.stat().st_size == meta["bytes"]:
            os.utime(dst)
            return dst
        lock = dst.with_suffix(".lock")
        for _ in range(600):                       # another worker may be downloading the same file
            if not lock.exists():
                break
            time.sleep(1)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch()
        try:
            self._fetch(f"dem_source={dem}/{slug}/{name}.tif", dst)
            if meta.get("sha256") and _sha256(dst) != meta["sha256"]:
                dst.unlink()
                raise RuntimeError(f"{dem}/{slug}/{name}.tif: checksum mismatch after download")
        finally:
            lock.unlink(missing_ok=True)
        self.evict()
        return dst

    def paths(self, dem: str, slug: str) -> dict[str, Path]:
        return {n: self.ensure(dem, slug, n) for n in FILES}

    def cache_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.cache.rglob("*.tif"))

    def evict(self) -> None:
        """Drop the least recently used district rasters when the cache exceeds CACHE_MAX_GB."""
        limit = settings.CACHE_MAX_GB * 2**30
        files = sorted(self.cache.rglob("*.tif"), key=lambda p: p.stat().st_atime)
        total = sum(p.stat().st_size for p in files)
        for p in files:
            if total <= limit:
                break
            total -= p.stat().st_size
            p.unlink(missing_ok=True)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
