"""Environment of the Flood Lab service (API and worker share it)."""
from __future__ import annotations

import os
from pathlib import Path

SERVICE_JWT_KEY = os.environ.get("SERVICE_JWT_KEY", "")
SERVICE_JWT_AUD = os.environ.get("SERVICE_JWT_AUD", "geoprocessing")
SERVICE_JWT_ISS = os.environ.get("SERVICE_JWT_ISS", "geohydro-platform")
SERVICE_JWT_ALG = os.environ.get("SERVICE_JWT_ALG", "HS256")
PLATFORM_REDIS_URL = os.environ.get("PLATFORM_REDIS_URL", "redis://platform-redis:6379/2")
QUEUE_KEY = os.environ.get("LAB_QUEUE_KEY", "lab_jobs")                      # legacy HTTP transport only
# Celery transport: the platform's broker (geoai-django CELERY_BROKER_URL), the Lab's queue and the platform's queue
BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://platform-redis:6379/0")
CELERY_QUEUE = os.environ.get("LAB_CELERY_QUEUE", "geoproc")
PLATFORM_QUEUE = os.environ.get("PLATFORM_CELERY_QUEUE", "celery")

S3_BUCKET = os.environ.get("S3_BUCKET", "geo-hydro-ai-data-116344514823-us-east-1-an")
TERRAIN_PREFIX = os.environ.get("TERRAIN_PREFIX", "hydro/lake/national_terrain/v1").strip("/")
# TERRAIN_LOCAL: a local copy of the staged tree (tests, the workstation); when set, S3 is never touched
TERRAIN_LOCAL = os.environ.get("TERRAIN_LOCAL", "")
CACHE_ROOT = Path(os.environ.get("CACHE_ROOT", "/data/lab/cache"))
CACHE_MAX_GB = float(os.environ.get("CACHE_MAX_GB", "8"))
JOBS_ROOT = Path(os.environ.get("JOBS_ROOT", "/data/lab/jobs"))
JOB_TTL_DAYS = int(os.environ.get("JOB_TTL_DAYS", "30"))

DEMS = tuple(d.strip() for d in os.environ.get("DEMS", "fab_dem,copernicus_dem").split(",") if d.strip())
PRIMARY_DEM = os.environ.get("PRIMARY_DEM", "fab_dem")
SNAP_DISTANCE_M = float(os.environ.get("SNAP_DISTANCE_M", "300"))
MAX_BASIN_KM2 = float(os.environ.get("MAX_BASIN_KM2", "50000"))
LEVELS_M = tuple(range(1, 11))
CHILD_MEM_BYTES = int(float(os.environ.get("CHILD_MEM_GB", "2.5")) * 2**30)
METHOD = "lab.national_d8.v1"
STREAM_THRESHOLD_CELLS = 2000
# the basin raster shipped to the platform with the result (base64 in the Celery event); bigger basins send stats only
RASTER_MAX_MB = float(os.environ.get("LAB_RASTER_MAX_MB", "8"))
STREAM_THRESHOLD_PRESET = "medium"     # the platform preset (low|medium|high) that 2000 cells corresponds to
