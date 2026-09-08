# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## READ FIRST

Before making any changes, read the relevant specification:

| Scope | Spec File |
|-------|-----------|
| System-wide architecture | `ARCHITECTURE.md` |
| Control Plane (orchestration) | `control_plane/control_plane/CORE_SPEC.md` |
| SAR Worker (SNAP pipeline) | `worker/SPEC.md` |
| DEM Worker (WhiteboxTools hydro) | `dem_worker/SPEC.md` |
| API (HTTP gateway) | `api/SPEC.md` |

**Specs are the source of truth for design decisions.** Code implements specs, not the other way around.

## Project Overview

Sentinel-1 SAR flood detection pipeline using a **compound mask**: backscatter (Otsu) + slope + HAND.

Microservice architecture with **contract-based orchestration** via a Control Plane.

Study area: Bukovyna region, Ukraine (June 2020 flood event).

## Architecture (Summary)

```
Client → Control Plane (FastAPI+PG) → Redis queues → Workers → cp_events → Control Plane
```

| Service | Container | Purpose |
|---------|-----------|---------|
| Control Plane | `flood_control_plane:8004` | State machine, contracts, orchestration |
| SAR Worker | `flood_worker` | SNAP pipeline + water mask (queues: flood_jobs, mask_jobs) |
| DEM Worker | `flood_dem_worker` | WhiteboxTools HAND+slope (queue: dem_jobs) |
| API | `flood_api:8000` | CDSE catalogue search + download trigger |
| Redis | `flood_redis:6379` | Job queues + DEM cache status |
| PostgreSQL | `flood_db:5432` | Run state persistence |

Pipeline: `flood.pipeline.v1` contract → SAR preprocessing → DEM derivatives → compound mask → water_mask.tif

## Critical Invariants (DO NOT VIOLATE)

1. **SNAP TC must use UTM** (EPSG:32635), never EPSG:4326 — causes `/ by zero`
2. **Materialize after Subset** — write BEAM-DIMAP + re-read to break lazy chain
3. **HAND/slope via WhiteboxTools only** — never custom NumPy D8 (previous attempt: 33,776m HAND)
4. **DEM derivatives cached per basin** — `/data/aux/{hash}/hand.tif + slope.tif`
5. **Workers emit events, Control Plane dispatches** — never the reverse
6. **All pipeline work starts with a contract** — no direct queue pushes from outside

## Contracts

| Contract | Purpose |
|----------|---------|
| `flood.pipeline.v1` | Full: scene → SAR → DEM → mask |
| `dem.hand.v1` | Standalone HAND + slope |
| `flood.mask.v1` | Standalone mask (requires cached HAND) |

## State Machine

```
CREATED → SAR_RUNNING → DEM_RUNNING → MASK_RUNNING → DONE
                                                       ↑
Any state + FAILED ────────────────────────────────→ FAILED
```

## Build & Run

```bash
docker compose build
docker compose up -d

# Submit a pipeline contract
curl -X POST http://localhost:8004/api/contracts -H 'Content-Type: application/json' -d '{
  "type": "flood.pipeline.v1",
  "spec_version": "v1",
  "payload": {
    "scene": "S1B_IW_GRDH_1SDV_20200611T043639_20200611T043704_021981_029B85_067B.SAFE",
    "basin_geojson": "/work/basin_bc.geojson"
  }
}'

# Check run status
curl http://localhost:8004/api/runs

# View logs
docker compose logs -f worker dem_worker control_plane
```

## Key Files

| File | Purpose |
|------|---------|
| `docker-compose.yml` | All services: redis, db, control_plane, api, snap, worker, dem_worker |
| `worker/worker.py` | SAR pipeline (esa_snappy) + compound mask |
| `dem_worker/wbt_pipeline.py` | WhiteboxTools: breach → D8 → streams → HAND + slope |
| `dem_worker/basin_utils.py` | basin_hash, extract_dem, warp_to_match |
| `control_plane/control_plane/domain/rules.py` | State machine (pure functions) |
| `control_plane/control_plane/domain/contracts/` | Contract schemas (Pydantic) |
| `control_plane/control_plane/infrastructure/dispatcher.py` | Redis queue dispatch |
| `basin_bc.geojson` | Bukovyna basin boundary |

## SNAP/esa_snappy Gotchas

- **Python 3.10 only** — esa_snappy is tested with SNAP 12 + Python 3.10
- **Dockerfile order**: `pip install esa_snappy` BEFORE `snappy-conf`
- **Sentinel-1 .zip**: Must unzip to `.SAFE` directory first
- **TC `/ by zero`**: Use UTM CRS for TC, gdalwarp to 4326 after
- **Lazy chain bug**: Materialize (write BEAM-DIMAP + re-read) after Subset
- **Memory**: Large scenes need `-Xmx4g -c 2G` flags

## Verification Checklist

After any pipeline change:

- [ ] HAND range: 0–50m (not 33,776m)
- [ ] Water %: 1–10% (not 0%, not 30%)
- [ ] TC CRS: EPSG:32635 (UTM), not EPSG:4326
- [ ] Final mask CRS: EPSG:4326
- [ ] DEM cache hit on 2nd run (same basin, different scene)
- [ ] Control Plane state reaches DONE
