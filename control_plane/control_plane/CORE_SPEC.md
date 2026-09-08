# Flood Control Plane — Core Architecture Specification (v1)

## 1. Purpose of the Core

The **Flood Control Plane** is a **policy engine and single source of truth** governing the lifecycle of all flood detection operations:

* SAR preprocessing (Sentinel-1 → calibrated, terrain-corrected backscatter)
* DEM derivative computation (HAND, slope)
* Water mask generation (compound threshold mask)

The core **does not execute geospatial workloads**.
It **does not run SNAP**, **does not run WhiteboxTools**, **does not touch rasters**.

> **Execution is external.**
> **Truth, policy, and state are internal.**

---

## 2. Fundamental Design Principles

### 2.1 Source of Truth

All system truth lives in **PostgreSQL**, not in workers, queues, or logs.

* No in-memory orchestration
* No hidden execution logic
* No implicit pipelines

If the system restarts, **state reconstruction is complete from the database**.

---

### 2.2 Separation of Concerns

| Layer | Responsibility |
|-------|---------------|
| Core (Control Plane) | Policy, state transitions, truth |
| Dispatcher | Translate actions into Redis queue pushes |
| SAR Worker | Execute SNAP pipeline, create water masks |
| DEM Worker | Execute WhiteboxTools pipeline, cache HAND/slope |
| API | Human/client interaction only |

No layer leaks responsibilities upward or downward.

---

## 3. Canonical Domain Model

The **entire orchestration kernel** is built around **one entity**.

### 3.1 `Run`

A **Run** represents a **single orchestration instance** (one pipeline execution).

A Run stores only:

* `id` — UUID, immutable identity
* `state` — current lifecycle state (enum)
* `contract` — the original declared intent (JSONB, immutable after creation)
* `artifacts` — facts produced during execution (JSONB, append-only)
* `created_at` / `updated_at` — timestamps

> If something cannot be expressed as **state, contract, or artifact**,
> it does not belong in the core.

---

## 4. High-Level Architecture

```
     Client / API
          │
     POST /contracts
          │
   ┌──────────────────┐
   │  Control Plane    │  ← policy & truth
   │  (FastAPI + PG)   │
   └────────┬─────────┘
            │
      next_action()
            │
   ┌──────────────────┐
   │  Dispatcher       │  ← thin Redis bridge
   │  (RPUSH to queue) │
   └────────┬─────────┘
            │
      Redis Queues (flood_jobs / dem_jobs / mask_jobs)
            │
      Workers (execute, produce artifacts)
            │
      RPUSH cp_events
            │
   ┌──────────────────┐
   │  Control Plane    │  ← event loop consumes cp_events
   │  (state machine)  │
   └──────────────────┘
```

---

## 5. Protocols

The system uses **two and only two protocols**:

1. **Contract Protocol** — what should be done
2. **Event Protocol** — what has happened

Both are **explicit**, **versioned**, and **immutable**.

---

## 6. Contract Protocol

### 6.1 Contract Envelope (Canonical)

```json
{
  "type": "flood.pipeline.v1",
  "spec_version": "v1",
  "idempotency_key": "optional",
  "payload": { }
}
```

**Properties:**

* `type` — globally unique contract identifier
* `spec_version` — pinned schema version (`v1`)
* `idempotency_key` — optional deduplication key
* `payload` — validated, canonical intent (Pydantic-enforced)

The core **never interprets free-form intent**.

---

## 7. Canonical Contracts (v1)

### 7.1 `flood.pipeline.v1`

**Semantic intent:** Process a Sentinel-1 scene through the full flood detection pipeline.

```json
{
  "type": "flood.pipeline.v1",
  "spec_version": "v1",
  "payload": {
    "scene": "S1B_IW_GRDH_1SDV_20200611T043639_20200611T043704_021981_029B85_067B.SAFE",
    "basin_geojson": "/work/basin_bc.geojson",
    "subset_wkt": "POLYGON((24.52 47.49, 25.33 47.49, 25.33 48.33, 24.52 48.33, 24.52 47.49))",
    "tc_epsg": "EPSG:32635",
    "stream_threshold": 1000,
    "water_threshold_min": 0.005,
    "water_threshold_max": 0.05,
    "max_slope_deg": 8.0,
    "max_hand_m": 25.0
  }
}
```

**Pipeline:** SAR_PREPROCESS → DEM_COMPUTE → WATER_MASK

**Expected artifacts:**

* `tc_tif` — path to terrain-corrected GeoTIFF (intermediate)
* `hand_tif` — path to cached HAND raster
* `slope_tif` — path to cached slope raster
* `basin_hash` — 12-char basin cache key
* `water_mask_tif` — path to final water mask (EPSG:4326)
* `scene` — original scene name

---

### 7.2 `dem.hand.v1`

**Semantic intent:** Compute HAND + slope for a basin from a DEM source (standalone).

```json
{
  "type": "dem.hand.v1",
  "spec_version": "v1",
  "payload": {
    "basin_geojson": "/work/basin_bc.geojson",
    "tc_tif": "/data/_tc_water_snap_S1B_IW_GRDH_....tif",
    "stream_threshold": 1000
  }
}
```

**Pipeline:** DEM_COMPUTE only (skips SAR and MASK)

**Expected artifacts:**

* `hand_tif` — `/data/aux/{hash}/hand.tif`
* `slope_tif` — `/data/aux/{hash}/slope.tif`
* `basin_hash` — cache key

---

### 7.3 `flood.mask.v1`

**Semantic intent:** Create a water mask from pre-computed inputs (standalone).

```json
{
  "type": "flood.mask.v1",
  "spec_version": "v1",
  "payload": {
    "tc_tif": "/data/_tc_xxx.tif",
    "hand_tif": "/data/aux/abc123def456/hand.tif",
    "slope_tif": "/data/aux/abc123def456/slope.tif",
    "basin_geojson": "/work/basin_bc.geojson",
    "output_tif": "/data/water_mask_xxx.tif",
    "water_threshold_min": 0.005,
    "water_threshold_max": 0.05,
    "max_slope_deg": 8.0,
    "max_hand_m": 25.0
  }
}
```

**Pipeline:** WATER_MASK only (skips SAR and DEM)

**Expected artifacts:**

* `water_mask_tif` — path to output mask

---

## 8. Event Protocol

### 8.1 Event Envelope

```json
{
  "run_id": "uuid",
  "event": "SAR_COMPLETED",
  "artifacts": {
    "tc_tif": "/data/_tc_xxx.tif"
  }
}
```

Events are **facts**, not commands.

### 8.2 Canonical Events

| Event | Emitter | Meaning |
|-------|---------|---------|
| `CONTRACT_ACCEPTED` | Control Plane (internal) | Contract validated, run created |
| `SAR_COMPLETED` | SAR Worker | SNAP pipeline finished, TC output ready |
| `DEM_COMPLETED` | DEM Worker | HAND + slope cached |
| `MASK_COMPLETED` | SAR Worker | Water mask written |
| `FAILED` | Any Worker | Unrecoverable error |

---

## 9. Run State Machine

### 9.1 States

| State | Meaning |
|-------|---------|
| `CREATED` | Run exists, contract validated |
| `SAR_RUNNING` | SNAP pipeline in progress |
| `SAR_READY` | TC output available (reserved for future use) |
| `DEM_RUNNING` | WhiteboxTools pipeline in progress |
| `DEM_READY` | HAND + slope cached (reserved for future use) |
| `MASK_RUNNING` | Compound mask in progress |
| `DONE` | Pipeline complete, water mask available |
| `FAILED` | Unrecoverable error |

### 9.2 Transition Policy (Declarative)

| Current State | Event | Next State | Action |
|--------------|-------|------------|--------|
| `CREATED` | `CONTRACT_ACCEPTED` | `SAR_RUNNING` | `SAR_PREPROCESS` |
| `SAR_RUNNING` | `SAR_COMPLETED` | `DEM_RUNNING` | `DEM_COMPUTE` |
| `DEM_RUNNING` | `DEM_COMPLETED` | `MASK_RUNNING` | `WATER_MASK` |
| `MASK_RUNNING` | `MASK_COMPLETED` | `DONE` | `NOTIFY` |
| `*` | `FAILED` | `FAILED` | — |

**Policy is data, not code.**

### 9.3 Standalone Contract Overrides

| Contract Type | Initial State | First Action |
|--------------|---------------|-------------|
| `flood.pipeline.v1` | `SAR_RUNNING` | `SAR_PREPROCESS` |
| `dem.hand.v1` | `DEM_RUNNING` | `DEM_COMPUTE` |
| `flood.mask.v1` | `MASK_RUNNING` | `WATER_MASK` |

---

## 10. Dispatcher

The Dispatcher is a **thin translation layer** from actions to Redis queue pushes.

| Action | Redis Queue | Consumer |
|--------|------------|----------|
| `SAR_PREPROCESS` | `flood_jobs` | SAR Worker |
| `DEM_COMPUTE` | `dem_jobs` | DEM Worker |
| `WATER_MASK` | `mask_jobs` | SAR Worker |
| `NOTIFY` | (log only) | — |

The Dispatcher **never** makes HTTP calls. All dispatch is via Redis RPUSH.

---

## 11. Event Loop (Background)

The Control Plane runs a **background asyncio task** that:

1. `BLPOP cp_events` (blocking, 5s timeout)
2. Deserializes event envelope
3. Loads Run from PostgreSQL
4. Merges artifacts
5. Applies `transition()` (pure function)
6. Computes `next_action()` (pure function)
7. Dispatches action if any
8. Commits to DB

This loop runs inside the same FastAPI process (lifespan context).

---

## 12. API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/contracts` | Submit a contract → create Run → dispatch first action |
| `POST` | `/events` | HTTP event ingestion (alternative to Redis queue) |
| `GET` | `/runs/{run_id}` | Get single run (state, contract, artifacts) |
| `GET` | `/runs` | List runs (filter by status, contract_type) |
| `GET` | `/health` | Health check |

---

## 13. Orchestration Rule

> **The core never executes.**
> **The core only decides.**

Workers:
* execute tasks
* produce artifacts
* emit events

The core:
* validates contracts
* stores truth
* applies policy
* issues next actions

---

## 14. Constraints for AI Agents

When modifying this system, an AI agent MUST:

1. **Never add geospatial computation to the Control Plane** — no rasterio, no GDAL, no numpy
2. **Never add orchestration logic to workers** — workers don't decide what runs next
3. **Never bypass the contract system** — all work starts with a contract
4. **Never bypass the event system** — all completion reports go through events
5. **Always add new contract types to the registry** — unregistered types are rejected
6. **Always add transition rules for new states** — unreachable states are bugs
7. **Always use Redis queues for dispatch** — never HTTP calls to workers
8. **Keep transition() and next_action() as pure functions** — no side effects, no DB access

---

## 15. Final Principle

> **The system is explainable by its data alone.**
> If you need to read code to understand state, the architecture is wrong.
>
> Contracts declare intent. Events declare facts.
> The state machine connects them.
> Everything else is plumbing.
