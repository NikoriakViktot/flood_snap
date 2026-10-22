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
TERRAIN_PREFIX = os.environ.get("TERRAIN_PREFIX", "hydro/lake/national_terrain/v2").strip("/")
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
STREAM_THRESHOLD_PRESET = "medium"     # the platform preset (low|medium|high) that 2000 cells corresponds to

# -- HAND models (lab_service/hand): one basin, one DEM, one stream-initiation threshold ---------------------------
PIPELINE_VERSION = "lab.hand.v1"
HAND_THRESHOLDS_KM2 = (0.5, 1.0, 2.0, 5.0)          # sensitivity options, not recommended values
HAND_WINDOW_BUFFER_M = float(os.environ.get("HAND_WINDOW_BUFFER_M", "1000"))   # DEM window = basin bbox + buffer
HAND_MAX_BASIN_KM2 = float(os.environ.get("HAND_MAX_BASIN_KM2", "2000"))       # inline delivery limit (M1)
HAND_PNG_MAX_PIXELS = int(os.environ.get("HAND_PNG_MAX_PIXELS", "4000000"))     # browser PNG budget (z12 grid)
HAND_PRODUCT_MAX_MB = float(os.environ.get("HAND_PRODUCT_MAX_MB", "8.5"))      # contract: content_b64 <= 12 M chars
HAND_EVENT_MAX_MB = float(os.environ.get("HAND_EVENT_MAX_MB", "40"))           # all products of one done event
# synthetic rating curves per reach (hand/src.py; FIM defaults: n 0.06, S_min 0.001, short = < 0.25 km² and < 500 m)
SRC_MANNING_N = float(os.environ.get("SRC_MANNING_N", "0.06"))
SRC_SLOPE_MIN = float(os.environ.get("SRC_SLOPE_MIN", "0.001"))
SRC_STAGE_STEP_M = float(os.environ.get("SRC_STAGE_STEP_M", "0.25"))
SRC_STAGE_MAX_M = float(os.environ.get("SRC_STAGE_MAX_M", "20"))
SRC_MIN_CATCHMENT_KM2 = float(os.environ.get("SRC_MIN_CATCHMENT_KM2", "0.25"))
SRC_MIN_LENGTH_M = float(os.environ.get("SRC_MIN_LENGTH_M", "500"))
PNG_ZOOM = 12                                        # WebMercatorQuad zoom of the browser PNGs (future XYZ tiles align)
PNG_MIN_ZOOM = int(os.environ.get("PNG_MIN_ZOOM", "10"))  # large basins step down to this zoom before falling back to COGs only

# -- rainfall–runoff runs (lab_service/gr4h): CemaNeige-GR4J (daily, the default) or CemaNeige-GR4H (hourly), airGR, on a
# delineated basin, as a short-lived Rscript child. The first model type of the Lab; others will be added.
GR4H_PIPELINE_VERSION = "lab.gr4.v2"
TIME_STEPS = ("1d", "1h")
DEFAULT_TIME_STEP = "1d"   # owner, 2026-10-17: our hourly precipitation is too uncertain to be the default
# daily parameter release (gr4h/params/releases.json): empty = the manifest's `default` (the release in operation).
# Switch to the ERA5-Land sets with GR4J_PARAMS_RELEASE=gr4j-era5land-2026.10; roll back by clearing it (or naming
# prut-network-gr4j-climuad-2026-10-16) and restarting the worker. A job may still name a release (params_release).
GR4J_PARAMS_RELEASE = os.environ.get("GR4J_PARAMS_RELEASE", "").strip()
GR4J_MAX_RUN_DAYS = int(os.environ.get("GR4J_MAX_RUN_DAYS", "366"))           # daily: a season or a year
GR4J_MAX_DAYS = int(os.environ.get("GR4J_MAX_DAYS", str(366 * 30)))           # daily forcing length
# memory guard (all Lab tasks): the host's MemAvailable before a job starts; below it the job waits and is retried
MIN_FREE_MEM_MB = int(os.environ.get("MIN_FREE_MEM_MB", "1536"))
MEM_RETRY_S = int(os.environ.get("MEM_RETRY_S", "300"))
MEM_RETRIES = int(os.environ.get("MEM_RETRIES", "4"))                         # 4 × 5 min = 20 min queued, then failed
GR4H_MIN_WARMUP_DAYS = int(os.environ.get("GR4H_MIN_WARMUP_DAYS", "365"))      # initial states come from the warm-up, not the event
GR4H_MAX_RUN_DAYS = int(os.environ.get("GR4H_MAX_RUN_DAYS", "31"))             # the event / forecast window
GR4H_MAX_HOURS = int(os.environ.get("GR4H_MAX_HOURS", str(24 * 366 * 3)))     # forcing length
GR4H_MAX_MEMBERS = int(os.environ.get("GR4H_MAX_MEMBERS", "1024"))             # parameter sets × hyetographs × depth factors
GR4H_TIMEOUT_S = int(os.environ.get("GR4H_TIMEOUT_S", "900"))
GR4H_FULL_SERIES_MAX_VALUES = int(os.environ.get("GR4H_FULL_SERIES_MAX_VALUES", "600000"))  # per-member series kept below this
# -- subbasins (lab_service/subbasins): QSWAT+-style delineation of one basin into a subbasin per stream link ------
SUBBASIN_PIPELINE_VERSION = "lab.subbasins.v1"
SUBBASIN_MAX_KM2 = float(os.environ.get("SUBBASIN_MAX_KM2", "12000"))           # window arrays must fit CHILD_MEM_GB
SUBBASIN_WINDOW_BUFFER_M = float(os.environ.get("SUBBASIN_WINDOW_BUFFER_M", "2000"))  # the burn may move a divide a little
SUBBASIN_BURN_DEPTH_M = 50.0                                                      # QSWAT+ default
SUBBASIN_THRESHOLD_KM2 = (1.0, 500.0)                                              # allowed stream-threshold range
SUBBASIN_DEFAULT_THRESHOLD_KM2 = 25.0                                              # the Prut prototype (218 subbasins to the border)
SUBBASIN_MAX_GAUGES = 300
SUBBASIN_MAX_RIVER_LINES = 20000
LAB_IMAGE_TAG = os.environ.get("LAB_IMAGE_TAG", "")  # provenance: the image this worker runs
DEM_LABELS = {
    "copernicus_dem": {"label": "Copernicus GLO-30", "version": "Copernicus DEM GLO-30 (2019 release, AWS Open Data mirror)",
                       "vertical_datum": "EGM2008", "horizontal_datum": "WGS84", "native_res": "1 arc-second",
                       "attribution": "© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under "
                                      "COPERNICUS by the European Union and ESA",
                       "licence": "Copernicus DEM licence (free for all users)"},
    "fab_dem": {"label": "FABDEM", "version": "FABDEM v1-2 (Hawker et al. 2022)", "vertical_datum": "EGM2008",
                "horizontal_datum": "WGS84", "native_res": "1 arc-second", "attribution": "FABDEM © University of Bristol",
                "licence": "CC BY-NC-SA 4.0 (non-commercial)"},
}
