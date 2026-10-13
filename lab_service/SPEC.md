# Flood Lab geoprocessing (`geoproc` + `geoproc-worker`) — Specification (v1)

## 1. Purpose

The Flood Lab service delineates a **river basin above an outlet** on the national terrain
products (D8, streams, HAND, DEM per river-basin district) and returns its statistics for two
DEMs (FABDEM and Copernicus) with a comparison.

It is an **internal** service: only the platform (Django, `geoai-django`) calls it, with a
service JWT. The browser never reaches it. Django's side of this contract is
`geohydro/adapters/gateway_geoproc_http.py` (client) and
`geohydro/adapters/tokens_jwt.py` (token).

> It does not store users, projects or permissions.
> It does not expose OpenAPI (`docs_url=None`).
> Job state is JSON files under `JOBS_ROOT`; the queue is one Redis list.

Code: `lab_service/` (`api.py`, `worker.py`, `pipeline.py`, `terrain.py`, `store.py`,
`callbacks.py`, `auth.py`, `settings.py`, `cli.py`), ~800 lines.

---

## 2. Components

| Component | Command | Role |
|---|---|---|
| `geoproc` | `uvicorn lab_service.api:app --workers 1` (port 8000) | Validates requests, writes the job record, `LPUSH` to the queue, answers status and cancel |
| `geoproc-worker` | `python3 -m lab_service.worker` | `BRPOP` one job at a time, runs the pipeline in a child process, writes results, calls the platform back |
| `lab_service.cli` | `python3 -m lab_service.cli --region 5.3.2 --lon … --lat …` | Same pipeline locally, no Redis/Django (development) |

```mermaid
sequenceDiagram
  participant D as platform-api (Django)
  participant A as geoproc (API)
  participant R as Redis list lab_jobs
  participant W as geoproc-worker
  participant S as S3 national_terrain/v1
  D->>A: POST /internal/jobs/basin-delineation (Bearer service JWT)
  A->>A: validate DEM, region, outlet; write JOBS_ROOT/<job_id>.json
  A->>R: LPUSH job_id
  A-->>D: 202 {status: queued}
  W->>R: BRPOP (30 s)
  W->>S: fetch d8/streams/hand/dem of the district (cached, sha256-checked)
  W->>W: child process: snap → watershed → clip → stats → polygonise, per DEM
  W-->>D: POST callback_url (running 5 %, stages, done/failed) with callback_token
  D->>A: GET /internal/jobs/{job_id} (re-poll every 60 s if a callback was missed)
```

---

## 3. HTTP API

All routes except health require `Authorization: Bearer <jwt>` signed with `SERVICE_JWT_KEY`
(HS256, audience `geoprocessing`, issuer `geohydro-platform`). Missing or bad token → 401;
empty key on the server → 503.

### 3.1 `POST /internal/jobs/basin-delineation` → 202

Request body (`BasinParams`):

| Field | Type | Rule |
|---|---|---|
| `job_id` | string | required, supplied by the caller (Django's job UUID); duplicate → 409 |
| `project_id` | string? | stored only |
| `region` | string | required; district code (`5.3.2`) or slug; unknown → 400 with the list of codes |
| `outlet_lon`, `outlet_lat` | float | WGS84; must fall inside `region` → else 400 |
| `hand_threshold` | float = 5 | echoed as `stats.levels_preview` |
| `stream_threshold` | string = `medium` | echoed only; the pipeline uses 2000 cells |
| `dem_cog_url` | string = `national:fab_dem` | must be `national:<dem>` with `<dem>` in `DEMS` → else 400 |
| `callback_url`, `callback_token` | string? | where progress and the result are POSTed |

503 when a DEM in `DEMS` has no published manifest in S3 ("not published yet").

Response: `{job_id, status: "queued", progress_pct: 0, error: null, result_ref: null}`.

### 3.2 `GET /internal/jobs/{job_id}` → 200 | 404

The same body the callback sends (`public_view`):

```json
{"job_id": "…", "status": "queued|running|done|failed|cancelled", "progress_pct": 0-100,
 "stage": "terrain|fab_dem|copernicus_dem|compare|null", "error": null,
 "result_ref": "lab:<job_id> (done only)", "result": {…}, "products": []}
```

### 3.3 `POST /internal/jobs/{job_id}/cancel` → 200 | 409

Only a `queued` job can be cancelled (`LREM` from the list, status `cancelled`). A running job
cannot be interrupted → 409.

### 3.4 `GET /internal/health` (no auth)

`{ok, dems: {fab_dem: true, copernicus_dem: true}, districts: 16, queue_depth, cache_gb, method}`.
`ok` is true only when Redis answers and every DEM manifest is readable. Compose uses it as the
healthcheck (30 s, 3 retries).

### 3.5 Result shape (`status: done`)

- `basin` — GeoJSON Polygon/MultiPolygon, EPSG:4326, 6 decimals; `snapped_outlet` — Point.
- `stats`: `method` (`lab.national_d8.v1`), `region`, `district_slug`, `district_name_ua`,
  `primary_dem`, `stream_threshold_cells` (2000), `snap_distance_m`, `levels_preview`,
  `area_km2`, `relief_m`, `main_channel_km`, `by_dem{<dem>: …}`, `comparison`.
- Per DEM: `area_km2`, `cells`, `elev_min_m`, `elev_max_m`, `mean_elev_m`, `relief_m`,
  `hypsometry` (21 points), `hand_at_outlet_m`, `mean_hand_m`, `hand_hist` (20 bins),
  `flood_levels` (1…10 m: area, share), `stream_cells`, `stream_length_km`,
  `drainage_density_km_km2`, `main_channel_km`, `quality{edge_touch, warnings[]}`.
- `comparison`: `iou`, `area_ratio_fab_to_cop`, `relief_diff_m`, `flood_area_diff_km2[]`,
  `secondary_basin` (simplified 45 m).

Error messages the user sees as they are: `LabError` texts, "out of memory: the basin is too
large for the Lab", "out of memory (worker killed)".

---

## 4. Callback contract (service → platform)

`POST callback_url` with `Authorization: Bearer <callback_token>`, JSON = `public_view` of
the job, timeout 20 s. Sent on start (`running`, 5 %), after each stage (10/40/70/90 %) and at
the end (100 %, `done` or `failed`). Up to 3 attempts (sleeps 2, 4, 6 s); any 4xx except 409
stops the retries; failures are logged only. The platform is expected to re-poll `GET
/internal/jobs/{id}` every 60 s, so a lost callback delays the result but does not lose it.

---

## 5. Worker

- **Queue**: Redis list `LAB_QUEUE_KEY` (default `lab_jobs`) on `PLATFORM_REDIS_URL`
  (default `redis://platform-redis:6379/2`). One job at a time; `BRPOP` timeout 30 s.
- **Isolation**: one `multiprocessing` (spawn) child per job with
  `RLIMIT_AS = CHILD_MEM_GB × 2^30` (2.5 GB default, 2.0 in prod compose). Messages back:
  `progress`, `done`, `failed`. Exit −9 → "out of memory (worker killed)".
- **Pipeline** (EPSG:3035): snap the outlet to the nearest stream cell within `SNAP_DISTANCE_M`
  (300) → WhiteboxTools `watershed` on the district D8 → reject < 4 cells or > `MAX_BASIN_KM2`
  (50 000) → clip HAND/DEM/streams → statistics and `downslope_flowpath_length` (a failure only
  adds a warning) → polygonise. Runs for every DEM in `DEMS`, then `compare(primary, other)`.
- **Terrain cache** (`terrain.py`): `s3://S3_BUCKET/TERRAIN_PREFIX/` holds
  `_districts/index.json`, `_districts/<slug>.geojson`, `dem_source=<dem>/manifest.json` and
  `dem_source=<dem>/<slug>/{d8,streams,hand,dem}.tif`. Files are downloaded on first use into
  `CACHE_ROOT`, verified by sha256 from the manifest, guarded by a `.lock` (up to 600 s), evicted
  LRU by atime above `CACHE_MAX_GB` (8). Index/manifests refresh after 1 h, district GeoJSON
  after 24 h. `TERRAIN_LOCAL=<dir>` replaces S3 for development and tests.
- **Outputs**: `JOBS_ROOT/<job_id>.json` (atomic write), `JOBS_ROOT/<job_id>/basin_<dem>.geojson`,
  `result.json`; `_tmp/` removed afterwards. `store.sweep()` every ~1 h deletes records older than
  `JOB_TTL_DAYS` (30).
- **No retries, no wall-clock timeout, no requeue**: a job that was `running` when the worker
  died stays `running` forever (see §8).

---

## 6. Configuration (`settings.py`)

| Variable | Default | Note |
|---|---|---|
| `SERVICE_JWT_KEY` | "" | required; same key as the platform's `SERVICE_JWT_KEY` |
| `SERVICE_JWT_AUD` / `SERVICE_JWT_ISS` / `SERVICE_JWT_ALG` | `geoprocessing` / `geohydro-platform` / `HS256` | |
| `PLATFORM_REDIS_URL` | `redis://platform-redis:6379/2` | `platform-redis` must be reachable on the `web` network |
| `LAB_QUEUE_KEY` | `lab_jobs` | |
| `S3_BUCKET` | hard-coded bucket | `AWS_DEFAULT_REGION` is read by boto3; credentials from the instance role |
| `TERRAIN_PREFIX` | `hydro/lake/national_terrain/v1` | |
| `TERRAIN_LOCAL` | "" | local terrain tree instead of S3 |
| `CACHE_ROOT` / `CACHE_MAX_GB` | `/data/lab/cache` / 8 | |
| `JOBS_ROOT` / `JOB_TTL_DAYS` | `/data/lab/jobs` / 30 | |
| `DEMS` / `PRIMARY_DEM` | `fab_dem,copernicus_dem` / `fab_dem` | `primary_dem` actually comes from `dem_cog_url` |
| `SNAP_DISTANCE_M` / `MAX_BASIN_KM2` | 300 / 50000 | |
| `CHILD_MEM_GB` | 2.5 | prod compose sets 2.0 |

Constants: `METHOD = lab.national_d8.v1`, `STREAM_THRESHOLD_CELLS = 2000`, `LEVELS_M = 1…10`.
`deploy/server/lab.env.example` lists the variables an operator sets; the server copy is
`~/flood_lab/lab.env` (0600, outside git).

---

## 7. Deployment

- Image `geohydroai-lab:latest` from `lab_service/Dockerfile` (`FROM projects-dem_worker`,
  ubuntu 22.04 + GDAL/Whitebox + fastapi/uvicorn/pyjwt/boto3/redis/httpx).
- `deploy/server/docker-compose.lab.yml`: external network `web` (= `geoai_web`), volume
  `/data/lab`, `geoproc` 512 MB / 0.5 CPU, `geoproc-worker` 2560 MB / 1.0 CPU, both
  `restart: unless-stopped`.
- `.github/workflows/deploy-lab.yml` on push to `main` touching `lab_service/**`, `dem_worker/**`,
  the compose file or the workflow; runner `[self-hosted, Linux, X64]`; builds both images on the
  server, requires `~/flood_lab/lab.env`, `compose up -d --build`, then polls `/internal/health`
  (20 × 5 s).

---

## 8. Constraints and known gaps

### MUST
- Every request carries the service JWT; the browser never calls this service directly.
- The outlet is validated against the district polygon before a job is queued.
- A job record is written before the queue push, so a status read never 404s for an accepted job.
- The child process is memory-limited; the worker itself survives a child OOM.

### MUST NOT
- Run more than one job at a time per worker (host RAM budget).
- Serve results to anyone but the platform; results carry no ownership information.

### Known gaps (as of 2026-10-13)
1. A worker crash leaves the job `running` with no requeue and no timeout: add a lease
   (`started_at` + max duration) and a sweep that fails stale jobs.
2. Images are built on the production host; move the build to GitHub-hosted runners and pull.
3. The container runs as root; add `USER` and `cap_drop: [ALL]`.
4. `lab.env.example` does not list `SERVICE_JWT_AUD/ISS/ALG`, `LAB_QUEUE_KEY`, `TERRAIN_LOCAL`,
   `JOB_TTL_DAYS`, `PRIMARY_DEM`, `SNAP_DISTANCE_M`, `MAX_BASIN_KM2`.
5. `stream_threshold` is accepted but ignored (2000 cells fixed); either honour it or drop it from
   the contract on both sides.

---

## 9. Verification

```bash
pytest lab_service/tests            # not in the root testpaths; run explicitly
python3 -m lab_service.cli --region 5.3.2 --lon 25.93 --lat 48.29 --district-dir <local tree>
curl -s http://127.0.0.1:8000/internal/health   # inside the geoproc container
```

`tests/test_api.py` covers auth (401), DEM/region/outlet validation, 202 submit, 409 duplicate,
cancel, 404 and health with FakeRedis and `TERRAIN_LOCAL`. `tests/test_pipeline.py` covers
snapping, `run_one` statistics and polygon, `compare` (IoU 1.0) and the too-large check on a
synthetic 60×60 district (skipped without whitebox).
