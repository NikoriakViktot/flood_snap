# Flood Detection Pipeline — System Architecture Specification (v1)

## 1. System Purpose

This system detects **flood water extent** from Sentinel-1 SAR imagery using a compound mask approach:

```
SAR backscatter (Sigma0) + terrain slope + Height Above Nearest Drainage (HAND)
```

The system is a **microservice pipeline** where each service has a single responsibility,
communicates via Redis queues, and is orchestrated by a stateful Control Plane.

> **This is a data processing pipeline, not a web application.**
> Every design decision optimizes for **correctness of geospatial output**, not latency.

---

## 2. Invariants (MUST NOT be violated)

These are absolute constraints. Any change that violates them is architecturally wrong.

### 2.1 Separation of Execution and Orchestration

| Component | Does | Does NOT |
|-----------|------|----------|
| Control Plane | Validates contracts, transitions state, dispatches actions | Execute SNAP, WhiteboxTools, or any geospatial computation |
| SAR Worker | Runs SNAP pipeline, creates water masks | Decide what to process next, manage other workers |
| DEM Worker | Runs WhiteboxTools hydro pipeline | Touch SAR data, create water masks |
| API | Accepts user requests, queries Copernicus | Process imagery, manage state |

### 2.2 DEM Derivatives Are Per-Basin, Not Per-Scene

HAND and slope depend on the **drainage basin**, not the satellite scene.
100 scenes over the same basin reuse **one** HAND/slope computation.

```
Cache key: SHA256(basin_geometry)[:12]
Cache path: /data/aux/{hash}/hand.tif + slope.tif
```

**Corollary:** The DEM Worker MUST be a separate service. Embedding hydro computation in the SAR Worker violates the caching principle and creates redundant computation.

### 2.3 No Homebrew Hydrology

D8 flow accumulation, depression breaching, and HAND computation MUST use **WhiteboxTools** (peer-reviewed, tested).

**NEVER** implement custom D8, flow routing, or HAND algorithms in Python/NumPy.
Previous attempt produced 33,776m HAND values and 0% water detection.

### 2.4 SNAP Terrain Correction CRS

SNAP `Terrain-Correction` operator MUST use a **metric CRS** (UTM, e.g. EPSG:32635).

**NEVER** use EPSG:4326 with `pixelSpacingInMeter` — this causes a `/ by zero` error in SNAP 12 because degrees and meters are incompatible units.

Reprojection to EPSG:4326 happens **after** TC, via `gdalwarp`.

### 2.5 SNAP Lazy Evaluation Chain Break

After `Subset` operator, MUST **materialize** (write BEAM-DIMAP + re-read) before heavy operators (Calibration, TC). SNAP 12 esa_snappy has a lazy evaluation bug that corrupts tile boundaries otherwise.

### 2.6 Event-Driven, Not Polling-Driven

Workers report completion via **events** pushed to `cp_events` Redis queue.
The Control Plane reacts to facts, not guesses.

The only exception: SAR Worker polls `dem:{hash}:status` while waiting for DEM cache
(because it's the same pipeline run, not cross-service orchestration).

---

## 3. Services

```
┌──────────────┐    POST /contracts     ┌──────────────────┐
│   Client /   │ ─────────────────────> │  Control Plane   │
│   API        │ <───────────────────── │  :8004           │
└──────────────┘    {run_id, state}     │                  │
                                        │  PostgreSQL (DB) │
                                        │  Redis (events)  │
                                        └────────┬─────────┘
                                                 │
                              dispatch (Redis RPUSH)
                                                 │
                    ┌────────────────────────────┼──────────────────┐
                    │                            │                  │
                    v                            v                  v
          ┌─────────────────┐       ┌────────────────┐    ┌──────────────┐
          │   SAR Worker    │       │  DEM Worker    │    │  SAR Worker   │
          │  (flood_jobs)   │       │  (dem_jobs)    │    │  (mask_jobs)  │
          │                 │       │                │    │               │
          │  esa_snappy     │       │  WhiteboxTools │    │  rasterio     │
          │  SNAP 12        │       │  (Rust binary) │    │  gdalwarp     │
          └────────┬────────┘       └───────┬────────┘    └──────┬────────┘
                   │                        │                    │
                   │     cp_events          │   cp_events        │  cp_events
                   └────────────────────────┴────────────────────┘
                                            │
                                            v
                                   ┌──────────────────┐
                                   │  Control Plane   │
                                   │  (event loop)    │
                                   └──────────────────┘
```

### 3.1 Service Table

| Service | Container | Queue(s) | Tech Stack | Port |
|---------|-----------|----------|------------|------|
| Control Plane | `flood_control_plane` | `cp_events` (consume) | FastAPI, PostgreSQL, Redis | 8004 |
| SAR Worker | `flood_worker` | `flood_jobs`, `mask_jobs` | esa_snappy (SNAP 12), rasterio | — |
| DEM Worker | `flood_dem_worker` | `dem_jobs` | WhiteboxTools (Rust), rasterio | — |
| API | `flood_api` | — | FastAPI, Copernicus CDSE | 8000 |
| Redis | `flood_redis` | — | Redis 7 | 6379 |
| PostgreSQL | `flood_db` | — | PostgreSQL 16 | 5432 |

---

## 4. Data Flow

### 4.1 Full Pipeline (flood.pipeline.v1)

```
1. Client → POST /contracts {type: "flood.pipeline.v1", scene: "S1B_IW_..."}
2. Control Plane → creates Run(CREATED) → transitions to SAR_RUNNING
3. Control Plane → RPUSH flood_jobs {...}
4. SAR Worker:
   a. SNAP: Read → ThermalNoiseRemoval → Orbit → Subset → [materialize]
           → Calibration → Speckle → Terrain-Correction(UTM, saveDEM)
   b. Writes _tc_*.tif (Sigma0_VV band + elevation band)
   c. RPUSH cp_events {event: SAR_COMPLETED, tc_tif: "..."}
5. Control Plane → SAR_RUNNING→DEM_RUNNING → RPUSH dem_jobs {...}
6. DEM Worker:
   a. Extract elevation band → single-band DEM GeoTIFF
   b. WhiteboxTools: breach → D8 → flow_acc → streams → HAND + slope
   c. Cache to /data/aux/{hash}/hand.tif + slope.tif
   d. RPUSH cp_events {event: DEM_COMPLETED}
7. Control Plane → DEM_RUNNING→MASK_RUNNING → RPUSH mask_jobs {...}
8. SAR Worker:
   a. gdalwarp HAND+slope to match Sigma0 grid
   b. Otsu threshold on log10(Sigma0)
   c. Compound mask: (σ⁰ < otsu) AND (slope < 8°) AND (HAND < 25m)
   d. gdalwarp clip to basin + reproject to EPSG:4326
   e. RPUSH cp_events {event: MASK_COMPLETED, water_mask_tif: "..."}
9. Control Plane → MASK_RUNNING→DONE
```

### 4.2 Warm Cache (second scene, same basin)

Steps 6 is **skipped entirely**. DEM Worker is not invoked.
SAR Worker detects cache hit at `/data/aux/{hash}/hand.tif` and proceeds directly to compound mask.

> **This is the key optimization.** DEM derivatives are expensive (~5 min per basin).
> SAR preprocessing is per-scene (~15 min). 100 scenes = 100 SAR runs + 1 DEM run.

---

## 5. Redis Queues

| Queue | Producer | Consumer | Payload Schema |
|-------|----------|----------|---------------|
| `flood_jobs` | Control Plane | SAR Worker | `{run_id, scene, basin_geojson, ...}` |
| `dem_jobs` | Control Plane / SAR Worker | DEM Worker | `{run_id, basin_geojson, tc_tif}` |
| `mask_jobs` | Control Plane | SAR Worker | `{run_id, tc_tif, hand_tif, slope_tif, basin_geojson, output_tif}` |
| `cp_events` | All Workers | Control Plane | `{run_id, event, artifacts}` |

### 5.1 Redis Keys (Non-Queue)

| Key Pattern | Type | Purpose |
|------------|------|---------|
| `dem:{hash}:status` | string | DEM cache status: `pending\|processing\|done\|error` |
| `job:{scene}:status` | string | SAR job status: `processing\|done\|error` |
| `job:{scene}:result` | string | Path to output water mask |

---

## 6. Shared Volume

All services share `/data` via Docker volume.

```
/data/
├── sentinel1/          # Input: .SAFE scenes (downloaded)
├── aux/
│   └── {basin_hash}/   # Cached DEM derivatives
│       ├── hand.tif    #   Height Above Nearest Drainage
│       └── slope.tif   #   Slope in degrees
├── water_mask_*.tif    # Output: final water masks (EPSG:4326)
└── (intermediates)     # Cleaned up after processing
```

---

## 7. Contracts (Summary)

| Contract | Purpose | Triggers |
|----------|---------|----------|
| `flood.pipeline.v1` | Full scene → water mask | SAR → DEM → Mask |
| `dem.hand.v1` | Standalone HAND + slope | DEM only |
| `flood.mask.v1` | Standalone water mask | Mask only (requires cached HAND) |

See `control_plane/CORE_SPEC.md` for contract schemas.

---

## 8. Technology Constraints

### 8.1 Why WhiteboxTools (not GRASS, not SAGA, not custom)

- Peer-reviewed hydrological algorithms (Lindsay 2016)
- Single Rust binary — no JVM, no Python GIL bottleneck
- `breach_depressions` + `elevation_above_stream` = correct HAND
- Previous custom NumPy D8 implementation was **broken** (33,776m HAND)

### 8.2 Why esa_snappy (not SNAP GPT CLI)

- SNAP 12 GPT CLI has tile-boundary bugs with GRD products
- esa_snappy Python API allows per-operator control and materialization
- Lazy chain can be broken explicitly (write BEAM-DIMAP + re-read)

### 8.3 Why Redis Queues (not HTTP, not Celery, not RabbitMQ)

- Workers are long-running (15+ minutes per job) — HTTP timeouts are a problem
- Redis is already in the stack for job status
- BLPOP is simple, reliable, and supports priority (multi-queue)
- No need for Celery's complexity — we have < 5 worker types

### 8.4 Why PostgreSQL for Control Plane

- JSONB for contract/artifact storage — flexible schema evolution
- Crash recovery: state reconstruction from DB alone
- Query runs by state, contract type, date range

---

## 9. Extension Points

When adding new capabilities, follow this pattern:

1. **Define a contract** in `control_plane/domain/contracts/`
2. **Add states + events** in `states.py` / `events.py`
3. **Add transition rules** in `rules.py`
4. **Add dispatch route** in `dispatcher.py`
5. **Create/modify worker** that consumes the new queue

### 9.1 Planned Extensions

| Feature | Contract | Worker |
|---------|----------|--------|
| Copernicus GLO-30 DEM | `dem.glo30.v1` | DEM Worker (new download step) |
| Multi-temporal analysis | `flood.change.v1` | New Change Worker |
| Vectorization | `flood.vectorize.v1` | SAR Worker (new queue) |
| Basin auto-detection | `basin.detect.v1` | DEM Worker |

---

## 10. Anti-Patterns (MUST AVOID)

| Anti-Pattern | Why It's Wrong | Correct Approach |
|-------------|---------------|------------------|
| Computing HAND in SAR Worker | Redundant per scene, wrong domain | Use DEM Worker + cache |
| Custom D8 in NumPy | Broken, unverified, slow | Use WhiteboxTools |
| SNAP TC with EPSG:4326 | Division by zero bug | Use UTM, gdalwarp after |
| HTTP dispatch to workers | Timeouts on 15min jobs | Redis queues |
| Skipping subset materialization | Corrupted tiles in SNAP 12 | Write BEAM-DIMAP + re-read |
| Storing pipeline state in Redis only | Lost on restart | PostgreSQL via Control Plane |
| Worker deciding what to process next | Violates separation | Control Plane dispatches |

---

## 11. Verification Checklist

After any change, verify:

- [ ] HAND range is **0–50m** (not 33,776m)
- [ ] Water percentage is **1–10%** for test scene (not 0%, not 30%)
- [ ] Slope range is **0–45°** (not 0–90°)
- [ ] TC output CRS is UTM (EPSG:326xx), not EPSG:4326
- [ ] Final water mask CRS is EPSG:4326 (reprojected by gdalwarp)
- [ ] DEM cache hit on second run for same basin (no re-computation)
- [ ] Control Plane state reaches DONE (not stuck at intermediate state)

---

## 12. Final Principle

> **The pipeline is explainable by its data flow alone.**
> If you need to read code to understand what happens to a Sentinel-1 scene,
> the architecture is wrong.
>
> Contracts declare intent. Events declare facts. State tells you where you are.
> Everything else is implementation detail.
