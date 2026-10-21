"""The precomputed national terrain (scripts/national/stage_terrain.py) read from S3 through a disk cache.

Layout: <prefix>/_districts/{index.json,<slug>.geojson}, <prefix>/dem_source=<dem>/manifest.json,
<prefix>/dem_source=<dem>/<slug>/{d8,streams,dem}.tif (+ hand.tif in v1 only, + dem_f32.tif: Float32 DEM for HAND models). A district raster is downloaded on first use and kept
under CACHE_ROOT; its sha256 from the manifest is checked once after the download. TERRAIN_LOCAL points at a local
copy of the same tree (tests, the workstation) and bypasses S3.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.ops import transform as shp_transform
from shapely.prepared import prep

from lab_service import settings

FILES = ("d8", "streams", "dem")
# national HAND: only in terrain v1 (statistics of a delineated basin). From v2 (transboundary districts) HAND exists
# only as a Flood Lab model of a basin — built, validated and published there.
OPTIONAL = ("hand",)


class TerrainMissing(RuntimeError):
    """A district raster the manifest does not list (an optional file such as dem_f32 not staged yet)."""


def crs_of(geojson: dict) -> str:
    """EPSG code of a GeoJSON's legacy `crs` member ('urn:ogc:def:crs:EPSG::3035' -> 'EPSG:3035'); RFC 7946 = WGS84."""
    name = ((geojson.get("crs") or {}).get("properties") or {}).get("name", "")
    if "CRS84" in name or not name:
        return "EPSG:4326"
    return "EPSG:" + name.rsplit(":", 1)[-1]


def to_wgs84(geom, crs: str):
    """The staged district cut lines are EPSG:3035 (scripts/national/stage_terrain.py); outlets arrive as lon/lat.
    Without this every outlet was 'outside every river-basin district' (found 2026-10-13)."""
    if crs in ("EPSG:4326", "EPSG:4258"):
        return geom
    from pyproj import Transformer
    t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    return shp_transform(t.transform, geom)


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
        self._districts_lock = threading.Lock()   # one load at a time: /internal/health and requests arrive together
        self._manifests: dict[str, dict] = {}
        self._s3 = None

    # ---- fetching ------------------------------------------------------------------------------
    def _fetch(self, key: str, dst: Path, max_age_s: float | None = None) -> Path:
        """key relative to the terrain prefix → local file (cached).

        Each download goes to its own temporary file, so two requests refreshing the same key cannot rename each
        other's file away (FileNotFoundError on AWS, 2026-10-21). A stale copy is served when the refresh fails."""
        if dst.exists() and (max_age_s is None or time.time() - dst.stat().st_mtime < max_age_s):
            return dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=dst.parent, prefix=f".{dst.name}.", suffix=".part")
        os.close(fd)
        tmp = Path(name)
        try:
            if settings.TERRAIN_LOCAL:
                shutil.copyfile(Path(settings.TERRAIN_LOCAL) / key, tmp)
            else:
                if self._s3 is None:
                    import boto3
                    self._s3 = boto3.client("s3")
                self._s3.download_file(settings.S3_BUCKET, f"{settings.TERRAIN_PREFIX}/{key}", str(tmp))
            os.replace(tmp, dst)
        except Exception:
            tmp.unlink(missing_ok=True)
            if dst.exists():
                return dst
            raise
        return dst

    def _json(self, key: str, max_age_s: float = 3600) -> dict:
        return json.loads(self._fetch(key, self.cache / key, max_age_s).read_text())

    # ---- districts -----------------------------------------------------------------------------
    def districts(self) -> list[District]:
        return [d for d, _ in self._load_districts()]

    def _load_districts(self):
        if self._districts is not None:
            return self._districts
        with self._districts_lock:
            if self._districts is None:
                self._districts = self._read_districts()
        return self._districts

    def _read_districts(self):
        index = self._json("_districts/index.json")
        out = []
        for d in index:
            g = json.loads(self._fetch(f"_districts/{d['slug']}.geojson", self.cache / "_districts" / f"{d['slug']}.geojson", 86400).read_text())
            geom = to_wgs84(shape(g["features"][0]["geometry"] if g.get("type") == "FeatureCollection" else g.get("geometry", g)),
                            crs_of(g))
            out.append((District(d["code"], d["slug"], d["name_ua"], float(d["km2"])), prep(geom)))
        return out

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

    def entry(self, dem: str, slug: str) -> dict:
        e = next((d for d in self.manifest(dem)["districts"] if d["slug"] == slug), None)
        if e is None:
            raise TerrainMissing(f"district {slug} is not in the {dem} terrain manifest")
        return e

    def has(self, dem: str, slug: str, name: str) -> bool:
        """Whether the manifest lists `<name>.tif` for the district (optional files: dem_f32)."""
        try:
            return f"{name}.tif" in self.entry(dem, slug)["files"]
        except TerrainMissing:
            return False

    def ensure(self, dem: str, slug: str, name: str) -> Path:
        """Local path of one district raster, downloaded on first use and verified against the manifest."""
        meta = self.entry(dem, slug)["files"].get(f"{name}.tif")
        if meta is None:
            raise TerrainMissing(f"{name}.tif is not staged for district {slug} ({dem})")
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

    def paths(self, dem: str, slug: str, names: tuple[str, ...] | None = None) -> dict[str, Path]:
        """Local paths of the district rasters; by default the required files plus the optional ones the manifest lists."""
        if names is None:
            names = FILES + tuple(n for n in OPTIONAL if self.has(dem, slug, n))
        return {n: self.ensure(dem, slug, n) for n in names}

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
