# DEM Worker — Specification (v1)

## 1. Purpose

The DEM Worker computes **hydrological derivatives** from Digital Elevation Models:

* **HAND** (Height Above Nearest Drainage) — vertical distance to nearest stream
* **Slope** — terrain gradient in degrees

These derivatives are used by the SAR Worker to filter false positive water detections
(shadows on steep slopes, low-lying non-water areas).

> **This worker owns hydrology. It does not touch SAR data or water masks.**

---

## 2. Technology: WhiteboxTools

All hydrological computation uses **WhiteboxTools** (Lindsay, 2016) — a peer-reviewed,
Rust-compiled geospatial analysis platform.

### 2.1 Why WhiteboxTools

| Requirement | WhiteboxTools | Custom NumPy (previous) |
|------------|---------------|------------------------|
| D8 flow routing | Correct, handles flats/pits | Broken (33,776m HAND) |
| Depression handling | `breach_depressions` (optimal) | None |
| HAND computation | `elevation_above_stream` (verified) | `distance_transform_edt` (wrong metric) |
| Performance | Rust binary, parallel tiles | Python GIL, single thread |
| Peer review | Yes (Lindsay 2016, 2018) | No |

### 2.2 Build from Source

The Dockerfile builds WhiteboxTools from source via Rust toolchain:

```
Rust (rustup) → cargo build --release → /usr/local/bin/whitebox_tools
```

The `whitebox` Python package provides the API wrapper that calls this binary.

**NEVER** use `pip install whitebox` alone without the compiled binary —
it will silently download a pre-built binary that may not match the architecture.

---

## 3. Pipeline

```
Input: single-band DEM GeoTIFF (extracted from SNAP TC output)
                    │
  1. breach_depressions(fill_pits=True)
                    │
  2. d8_pointer ────────────────────────┐
                    │                   │
  3. d8_flow_accumulation(cells)        │
                    │                   │
  4. Stream raster: acc >= threshold    │
                    │                   │
  5. elevation_above_stream ────────────┘
         │                    │
      hand.tif            slope.tif (from step 6)
                              │
                   6. slope(zfactor=1.0)
```

### 3.1 Step Details

| Step | WBT Function | Input | Output | Notes |
|------|-------------|-------|--------|-------|
| 1 | `breach_depressions` | DEM | breached DEM | `fill_pits=True` handles both depressions and flats |
| 2 | `d8_pointer` | breached | flow direction | D8 algorithm: flow to steepest neighbor |
| 3 | `d8_flow_accumulation` | breached | flow accumulation | `out_type="cells"` — count of upstream cells |
| 4 | (rasterio/numpy) | flow acc | stream raster | Binary: `acc >= threshold → 1, else 0` |
| 5 | `elevation_above_stream` | breached + streams | **HAND** | Vertical drop to nearest stream via D8 path |
| 6 | `slope` | breached | **slope** | Degrees, `zfactor=1.0` (metric CRS) |

### 3.2 Stream Threshold

Default: **1000 cells** (configurable via `STREAM_THRESHOLD` env var).

At 10m pixel spacing: 1000 cells = 0.1 km² contributing area.

**Too low** (< 100): too many streams, HAND too low everywhere, over-detection of water.
**Too high** (> 5000): too few streams, HAND too high, under-detection of water.

---

## 4. Input/Output

### 4.1 Input

**Redis queue:** `dem_jobs`

```json
{
  "run_id": "uuid (optional)",
  "basin_geojson": "/work/basin_bc.geojson",
  "tc_tif": "/data/_tc_water_snap_S1B_IW_GRDH_xxx.tif"
}
```

* `tc_tif` — SNAP Terrain Correction output with elevation band (band 2)
* `basin_geojson` — basin boundary for cache key computation

### 4.2 Output

**Cached at:** `/data/aux/{basin_hash}/`

| File | Format | CRS | Description |
|------|--------|-----|-------------|
| `hand.tif` | Float32 GeoTIFF | Same as input DEM (UTM) | Height Above Nearest Drainage (metres) |
| `slope.tif` | Float32 GeoTIFF | Same as input DEM (UTM) | Slope (degrees) |

### 4.3 Expected Value Ranges

| Output | Valid Range | Alarm Range |
|--------|------------|-------------|
| HAND | 0 – 50 m (typical) | > 100 m suggests broken drainage |
| HAND | — | > 1000 m means pipeline is broken |
| Slope | 0 – 45° (typical) | > 60° is suspicious |

### 4.4 Event Emitted

```json
{
  "run_id": "uuid",
  "event": "DEM_COMPLETED",
  "artifacts": {
    "hand_tif": "/data/aux/abc123def456/hand.tif",
    "slope_tif": "/data/aux/abc123def456/slope.tif",
    "basin_hash": "abc123def456"
  }
}
```

Pushed to `cp_events` Redis queue.

---

## 5. Caching Strategy

### 5.1 Cache Key

```python
hash = SHA256(json.dumps(geometry, sort_keys=True, separators=(",",":"))).hexdigest()[:12]
```

* Deterministic: same basin geometry → same hash
* 12 chars: collision-safe for < 10,000 basins

### 5.2 Cache Lifecycle

```
dem:{hash}:status = pending    → DEM job enqueued
dem:{hash}:status = processing → DEM worker picked it up
dem:{hash}:status = done       → HAND + slope available
dem:{hash}:status = error      → Pipeline failed
```

### 5.3 Cache Hit

If `dem:{hash}:status == "done"` AND `/data/aux/{hash}/hand.tif` exists:
- Skip pipeline entirely
- Emit `DEM_COMPLETED` immediately

### 5.4 Cache Invalidation

Currently: **manual** (delete `/data/aux/{hash}/` and Redis key).

Future: TTL-based or triggered by new DEM source upload.

---

## 6. DEM Source

Current: **SRTM 3Sec elevation extracted from SNAP TC output** (band 2).

* Already downloaded by SNAP during Terrain Correction
* Already in UTM (EPSG:32635) at 10m pixel spacing
* No separate download needed

### 6.1 Upgrade Path: Copernicus GLO-30

Future `dem.glo30.v1` contract would:
1. Download GLO-30 tiles via Copernicus API
2. Mosaic + clip to basin
3. Feed to the same WBT pipeline

This requires adding `download_glo30(bbox)` to `basin_utils.py`.

---

## 7. Constraints

### 7.1 MUST

* Use WhiteboxTools for all hydrological computation
* Use `breach_depressions` (not `fill_depressions`) — breaching preserves drainage patterns
* Use `elevation_above_stream` for HAND — it follows D8 flow path, not Euclidean distance
* Cache results per basin hash
* Emit events to `cp_events` on completion
* Clean up intermediate files (_wbt_tmp directory)

### 7.2 MUST NOT

* Implement custom D8, flow accumulation, or HAND in Python
* Use `distance_transform_edt` for HAND (wrong: Euclidean, not hydrological distance)
* Use `fill_depressions` (creates flat areas, breaks flow routing)
* Touch SAR data (Sigma0) — that belongs to SAR Worker
* Compute water masks — that belongs to SAR Worker
* Make decisions about pipeline flow — that belongs to Control Plane

### 7.3 SHOULD

* Log HAND and slope value ranges after computation (for verification)
* Raise error if 0 stream cells found (threshold too high)
* Handle nodata values in DEM (mark as -9999, exclude from routing)

---

## 8. Docker

```dockerfile
FROM ubuntu:22.04
# Rust toolchain → build WhiteboxTools from source
# Python: redis, rasterio, numpy, whitebox
```

**No SNAP, no esa_snappy** — this worker is pure DEM processing.

Environment variables:

| Var | Default | Description |
|-----|---------|-------------|
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection |
| `DATA_DIR` | `/data` | Shared volume root |
| `STREAM_THRESHOLD` | `1000` | Flow accumulation threshold |

---

## 9. Files

| File | Purpose |
|------|---------|
| `wbt_pipeline.py` | Pure function: `run_dem_pipeline(dem_tif, output_dir, threshold) → (hand, slope)` |
| `basin_utils.py` | Shared utilities: `basin_hash()`, `extract_dem_from_snap_tc()`, `warp_to_match()` |
| `dem_worker.py` | Redis consumer: BLPOP dem_jobs → process → RPUSH cp_events |
| `Dockerfile` | Builds WhiteboxTools from source, installs Python deps |
| `requirements.txt` | Python dependencies |

---

## 10. Verification

After any change to this worker:

```
HAND range:  0 – 50 m (typical for Bukovyna basin)
             NEVER > 1000 m
Slope range: 0 – 45° (mountainous region)
             NEVER > 90°
Stream %:    0.1 – 5% of DEM cells
             NEVER 0% (threshold too high)
             NEVER > 30% (threshold too low)
```
