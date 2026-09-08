# SAR Worker — Specification (v1)

## 1. Purpose

The SAR Worker processes **Sentinel-1 GRD imagery** through two phases:

1. **SAR Preprocessing** (SNAP pipeline): raw .SAFE → calibrated, terrain-corrected backscatter
2. **Water Mask Generation**: compound threshold mask using backscatter + slope + HAND

> **This worker owns SAR processing and mask creation.**
> **It does NOT compute HAND or slope** — those come from the DEM Worker cache.

---

## 2. Technology: ESA SNAP 12 via esa_snappy

SAR preprocessing uses **esa_snappy** — the Python API for ESA SNAP.

### 2.1 Why esa_snappy (not GPT CLI)

* SNAP 12 GPT CLI has tile-boundary bugs with Sentinel-1 GRD products
* esa_snappy allows per-operator control and lazy chain materialization
* We can break the lazy evaluation chain explicitly (mandatory for correct output)

### 2.2 Critical SNAP Bugs (MUST handle)

| Bug | Symptom | Fix |
|-----|---------|-----|
| Lazy evaluation chain | Corrupted tile boundaries after Subset | **Materialize** (write BEAM-DIMAP + re-read) after Subset |
| TC with EPSG:4326 | `/ by zero` in Terrain-Correction | Use **UTM** (EPSG:32635) for TC, gdalwarp to 4326 after |

These are not optional workarounds. **Removing them breaks the pipeline.**

---

## 3. SNAP Pipeline (8 Steps)

```
Input: S1B_IW_GRDH_*.SAFE (Sentinel-1 GRD, IW mode, VV+VH)

Step 1: ProductIO.readProduct
Step 2: ThermalNoiseRemoval (VV only)
Step 3: Apply-Orbit-File (Sentinel Precise, continueOnFail=true)
Step 4: Subset (AOI WKT) → MATERIALIZE (write BEAM-DIMAP + re-read)
Step 5: Calibration (Sigma0, linear scale)
Step 6: Speckle-Filter (Lee Sigma 3×3)
Step 7: Terrain-Correction (SRTM 3Sec, EPSG:32635, 10m, saveDEM=true)
Step 8: Write GeoTIFF (_tc_*.tif: band 1 = Sigma0_VV, band 2 = elevation)
```

### 3.1 Operator Parameters

| Operator | Key Parameters | Rationale |
|----------|---------------|-----------|
| ThermalNoiseRemoval | `selectedPolarisations=VV` | VV is primary for water detection |
| Apply-Orbit-File | `continueOnFail=true` | Fallback to predicted orbits if precise unavailable |
| Subset | `geoRegion=WKT` | Reduce data volume before heavy operators |
| Calibration | `outputSigmaBand=true, outputImageScaleInDb=false` | Linear scale for Otsu threshold |
| Speckle-Filter | `Lee Sigma, 3×3` | Moderate noise reduction without over-smoothing |
| Terrain-Correction | `SRTM 3Sec, EPSG:32635, 10m, saveDEM=true` | UTM avoids /by zero; saveDEM provides elevation band |

### 3.2 Why saveDEM=true

The elevation band from TC output is used as DEM input for WhiteboxTools.
This avoids a separate DEM download — SNAP already fetches SRTM during TC.

---

## 4. Compound Water Mask

### 4.1 Algorithm

```
mask = (σ⁰ < otsu_threshold) AND (slope < 8°) AND (HAND < 25m)
```

Where:
* `σ⁰` — Sigma0_VV in linear scale (from SNAP TC output)
* `otsu_threshold` — automatic threshold via Otsu on log10(σ⁰)
* `slope` — from DEM Worker cache (gdalwarp'd to match σ⁰ grid)
* `HAND` — from DEM Worker cache (gdalwarp'd to match σ⁰ grid)

### 4.2 Otsu Threshold

1. Extract valid σ⁰ pixels (> 0, finite)
2. Transform to log10 space (SAR backscatter is log-normal)
3. Compute Otsu threshold on 256-bin histogram
4. Convert back to linear scale
5. Clamp to [0.005, 0.05] for safety
6. Fallback: 0.017 if < 1000 valid pixels

### 4.3 Why Compound (not just Otsu)

| Approach | Problem |
|----------|---------|
| Otsu only | False positives: radar shadow on mountains looks like water |
| Otsu + slope | Better, but misses elevated flat areas (parking lots, airports) |
| Otsu + slope + HAND | Correct: water must be in valley AND flat AND dark in SAR |

### 4.4 Grid Alignment

HAND and slope rasters from DEM Worker may have different extent/resolution than σ⁰.
Before mask computation, both are **warped to match** the σ⁰ grid:

```bash
gdalwarp -t_srs {crs} -te {bounds} -tr {res} -r bilinear hand.tif warped_hand.tif
```

---

## 5. Input/Output

### 5.1 Redis Queues

| Queue | Job Type | Payload |
|-------|----------|---------|
| `flood_jobs` | Full pipeline | `{run_id, scene, basin_geojson, ...}` |
| `mask_jobs` | Mask only | `{run_id, tc_tif, hand_tif, slope_tif, basin_geojson, output_tif}` |

### 5.2 Input

* Sentinel-1 .SAFE directory at `/data/sentinel1/{scene}.SAFE`
* Or .zip at `/data/sentinel1/{scene}.zip` (auto-extracted)
* Basin GeoJSON at `BASIN_GEOJSON` env var (default: `/work/basin_bc.geojson`)

### 5.3 Output

| File | Format | CRS | Description |
|------|--------|-----|-------------|
| `water_mask_{scene}.tif` | uint8 GeoTIFF | EPSG:4326 | 1=water, 0=land, 255=nodata |

Intermediate files (TC output, warped HAND/slope, subset) are cleaned up after processing.

### 5.4 Events Emitted

| Event | When | Artifacts |
|-------|------|-----------|
| `SAR_COMPLETED` | After SNAP step 8 | `{tc_tif: "..."}` |
| `MASK_COMPLETED` | After compound mask + clip | `{water_mask_tif: "...", scene: "..."}` |
| `FAILED` | On unrecoverable error | `{error: "..."}` |

All events pushed to `cp_events` Redis queue.

---

## 6. DEM Cache Integration

### 6.1 Cache Check

Before computing compound mask, the worker checks:

```python
if exists(/data/aux/{hash}/hand.tif) and redis.get(f"dem:{hash}:status") == "done":
    # Cache hit → proceed directly to mask
else:
    # Cache miss → enqueue dem_jobs + poll until ready
```

### 6.2 Cache Miss Flow

1. Set `dem:{hash}:status = "pending"` in Redis
2. RPUSH `dem_jobs` with `{basin_geojson, tc_tif, run_id}`
3. Poll `dem:{hash}:status` every 5 seconds
4. Timeout after 600 seconds (10 minutes)
5. On `done` → proceed with mask
6. On `error` → raise, job fails

### 6.3 Cache Hit Flow

Skip DEM worker entirely. Read HAND/slope directly from `/data/aux/{hash}/`.

---

## 7. Thresholds

| Parameter | Value | Env Override | Rationale |
|-----------|-------|-------------|-----------|
| `WATER_THRESHOLD_FALLBACK` | 0.017 | — | Safe default for Sigma0_VV |
| `WATER_THRESHOLD_MIN` | 0.005 | — | Otsu clamp lower bound |
| `WATER_THRESHOLD_MAX` | 0.05 | — | Otsu clamp upper bound |
| `MAX_SLOPE_DEG` | 8° | — | Flat terrain constraint |
| `MAX_HAND_M` | 25 m | — | Valley proximity constraint |
| `DEM_POLL_INTERVAL` | 5 s | — | DEM cache poll frequency |
| `DEM_POLL_TIMEOUT` | 600 s | — | Max wait for DEM worker |

---

## 8. Constraints

### 8.1 MUST

* Use esa_snappy Python API (not GPT CLI)
* Materialize after Subset (write BEAM-DIMAP + re-read)
* Use UTM CRS (EPSG:326xx) for Terrain-Correction
* Use `saveDEM=true` in TC to include elevation band
* Reproject to EPSG:4326 via `gdalwarp` (after TC, after mask)
* Use HAND + slope from DEM Worker cache (never compute locally)
* Emit events to `cp_events` for both SAR_COMPLETED and MASK_COMPLETED
* Clean up all intermediate files after processing

### 8.2 MUST NOT

* Implement D8 flow accumulation, HAND, or slope computation
* Import scipy (removed — was used for broken `distance_transform_edt` HAND)
* Use EPSG:4326 in `Terrain-Correction` operator
* Skip lazy chain materialization after Subset
* Decide what to process next — that's the Control Plane's job
* Write to PostgreSQL — that's the Control Plane's job

### 8.3 SHOULD

* Log Otsu threshold value (for debugging)
* Log water percentage (for verification: expect 1-10%)
* Log HAND/slope ranges after warping (for verification)
* Handle missing .SAFE gracefully (check .zip, check alt paths)

---

## 9. Docker

```dockerfile
FROM flood-snap:latest    # Base image with SNAP 12 + esa_snappy
RUN pip install redis geopandas rasterio
```

**Requires** `flood-snap:latest` image (built from `snap/Dockerfile`).

Environment variables:

| Var | Default | Description |
|-----|---------|-------------|
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection |
| `DATA_DIR` | `/data` | Shared volume root |
| `BASIN_GEOJSON` | `/work/basin_bc.geojson` | Default basin boundary |

---

## 10. Files

| File | Purpose |
|------|---------|
| `worker.py` | Main: Redis consumer, SNAP pipeline, compound mask, event emission |
| `basin_bc.geojson` | Default basin boundary (copied into container) |
| `Dockerfile` | Based on flood-snap, adds redis/geopandas/rasterio |

---

## 11. Verification

After any change:

```
Otsu threshold: 0.005 – 0.05 (linear scale)
                NEVER 0 or negative
Water %:        1 – 10% for Bukovyna test scene
                NEVER 0% (mask broken)
                NEVER > 30% (threshold too permissive)
TC output CRS:  EPSG:32635 (UTM zone 35N)
                NEVER EPSG:4326
Final mask CRS: EPSG:4326
                Always reprojected via gdalwarp
Band count:     TC output MUST have 2+ bands (Sigma0 + elevation)
                If only 1 band → saveDEM not enabled
```

---

## 12. Performance Profile

| Stage | Duration | Notes |
|-------|----------|-------|
| SNAP pipeline (steps 1-8) | 10-20 min | Dominated by TC (SRTM download + resampling) |
| DEM cache wait (cold) | 3-8 min | First time only; subsequent scenes skip |
| DEM cache wait (warm) | 0 s | Cache hit |
| Compound mask | < 30 s | Pure numpy + rasterio |
| gdalwarp clip | < 10 s | Reproject + clip to basin |
| **Total (cold cache)** | **15-30 min** | |
| **Total (warm cache)** | **12-22 min** | |
