"""
SAR Worker: Sentinel-1 GRD → water mask via esa_snappy + cached HAND/slope.

Listens on two Redis queues:
  - flood_jobs: full SAR pipeline (SNAP preprocessing → TC output)
  - mask_jobs:  compound mask only (requires pre-cached HAND/slope)

Pipeline: Read → ThermalNoiseRemoval(VV) → Apply-Orbit-File → Subset(AOI)
  → Calibration(Sigma0) → Speckle-Filter(Lee Sigma 3x3)
  → Terrain-Correction(SRTM, UTM/EPSG:32635, 10m, saveDEM)
  → Write GeoTIFF → compound mask (Otsu + slope + HAND)
  → gdalwarp clip + reproject to EPSG:4326.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
import rasterio
import redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("worker")

# ---------------------------------------------------------------------------
# esa_snappy bootstrap
# ---------------------------------------------------------------------------
import esa_snappy  # noqa: E402
from esa_snappy import ProductIO, GPF, HashMap, jpy  # noqa: E402

JDouble = jpy.get_type("java.lang.Double")
JInteger = jpy.get_type("java.lang.Integer")
JString = jpy.get_type("java.lang.String")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
AUX_DIR = DATA_DIR / "aux"
SENTINEL_DIR = DATA_DIR / "sentinel1"
BASIN_GEOJSON = Path(os.environ.get("BASIN_GEOJSON", "/work/basin_bc.geojson"))

SUBSET_WKT = "POLYGON((24.52 47.49, 25.33 47.49, 25.33 48.33, 24.52 48.33, 24.52 47.49))"
TC_EPSG = "EPSG:32635"

# Compound mask thresholds
WATER_THRESHOLD_FALLBACK = 0.017
WATER_THRESHOLD_MIN = 0.005
WATER_THRESHOLD_MAX = 0.05
MAX_SLOPE_DEG = 8
MAX_HAND_M = 25

# DEM worker polling
DEM_POLL_INTERVAL = 5     # seconds
DEM_POLL_TIMEOUT = 600    # seconds


def connect_redis() -> redis.Redis:
    """Connect to Redis with retry. Delegates to shared.redis_helpers if available."""
    try:
        from shared.redis_helpers import connect_redis as _connect
        return _connect(REDIS_URL)
    except ImportError:
        pass
    for attempt in range(30):
        try:
            r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
            r.ping()
            log.info("Connected to Redis")
            return r
        except redis.ConnectionError:
            log.warning("Redis not ready, retry %d/30 …", attempt + 1)
            time.sleep(2)
    raise RuntimeError("Cannot connect to Redis")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def basin_hash(geojson_path: str | Path) -> str:
    """Compute basin hash. Delegates to shared.basin if available."""
    try:
        from shared.basin import basin_hash as _bh
        return _bh(geojson_path)
    except ImportError:
        pass
    import hashlib
    with open(geojson_path) as f:
        data = json.load(f)
    if data.get("type") == "FeatureCollection":
        geom = data["features"][0]["geometry"]
    elif data.get("type") == "Feature":
        geom = data["geometry"]
    else:
        geom = data
    raw = json.dumps(geom, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def unzip_if_needed(scene_name: str) -> Path:
    safe_dir = SENTINEL_DIR / scene_name
    if safe_dir.exists():
        return safe_dir

    zip_path = SENTINEL_DIR / (scene_name.replace(".SAFE", ".zip"))
    if zip_path.exists():
        log.info("Unzipping %s …", zip_path.name)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(SENTINEL_DIR)
        if safe_dir.exists():
            return safe_dir

    alt = DATA_DIR / scene_name
    if alt.exists():
        return alt

    raise FileNotFoundError(f"Cannot find scene: {scene_name}")


def _params(**kwargs) -> HashMap:
    p = HashMap()
    for k, v in kwargs.items():
        if isinstance(v, float):
            p.put(k, JDouble(v))
        elif isinstance(v, int):
            p.put(k, JInteger(v))
        elif isinstance(v, bool):
            p.put(k, v)
        else:
            p.put(k, str(v))
    return p


# ---------------------------------------------------------------------------
# Otsu threshold (kept from original)
# ---------------------------------------------------------------------------

def auto_detect_threshold(sigma0: np.ndarray) -> float:
    """Otsu threshold on log10(sigma0), clamped to [0.005, 0.05].

    Delegates to engines.otsu for the pure computation.
    """
    from engines.otsu import auto_detect_threshold as _otsu
    return _otsu(
        sigma0,
        fallback=WATER_THRESHOLD_FALLBACK,
        threshold_min=WATER_THRESHOLD_MIN,
        threshold_max=WATER_THRESHOLD_MAX,
    )


# ---------------------------------------------------------------------------
# Compound mask using cached HAND/slope from DEM worker
# ---------------------------------------------------------------------------

def wait_for_dem(bhash: str, r: redis.Redis, timeout: int = DEM_POLL_TIMEOUT) -> Path:
    """Wait for DEM worker to finish, return aux dir path."""
    status_key = f"dem:{bhash}:status"
    out_dir = AUX_DIR / bhash

    start = time.time()
    while time.time() - start < timeout:
        status = r.get(status_key)
        if status == "done" and (out_dir / "hand.tif").exists():
            return out_dir
        if status == "error":
            raise RuntimeError(f"DEM worker failed for basin {bhash}")
        log.info("  Waiting for DEM worker (basin=%s, status=%s) …", bhash, status)
        time.sleep(DEM_POLL_INTERVAL)

    raise TimeoutError(f"DEM worker timeout ({timeout}s) for basin {bhash}")


def warp_to_match(src_tif: Path, ref_tif: Path, output_tif: Path) -> Path:
    """Warp src to match ref grid (CRS, extent, resolution)."""
    with rasterio.open(ref_tif) as ref:
        bounds = ref.bounds
        res_x = ref.transform.a
        res_y = -ref.transform.e
        crs = ref.crs.to_string()

    cmd = [
        "gdalwarp", "-overwrite",
        "-t_srs", crs,
        "-te", str(bounds.left), str(bounds.bottom),
              str(bounds.right), str(bounds.top),
        "-tr", str(abs(res_x)), str(abs(res_y)),
        "-r", "bilinear",
        str(src_tif), str(output_tif),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return output_tif


def create_compound_mask(
    tc_tif: Path,
    output_tif: Path,
    basin_geojson: Path,
    r: redis.Redis,
    run_id: str | None = None,
):
    """Create compound water mask using cached HAND/slope from DEM worker.

    Compound mask: (sigma0 < otsu) AND (slope < 8deg) AND (hand < 25m)
    """
    bhash = basin_hash(basin_geojson)
    out_dir = AUX_DIR / bhash

    # Check cache or enqueue DEM job + wait
    if not (out_dir / "hand.tif").exists() or r.get(f"dem:{bhash}:status") != "done":
        log.info("  HAND/slope not cached for basin %s — requesting DEM worker", bhash)
        r.set(f"dem:{bhash}:status", "pending")
        r.rpush("dem_jobs", json.dumps({
            "basin_geojson": str(basin_geojson),
            "tc_tif": str(tc_tif),
            "run_id": run_id,
        }))
        out_dir = wait_for_dem(bhash, r)

    hand_tif = out_dir / "hand.tif"
    slope_tif = out_dir / "slope.tif"
    log.info("  Using cached HAND/slope from %s", out_dir)

    # Read TC output
    with rasterio.open(tc_tif) as src:
        descriptions = [src.descriptions[i] if src.descriptions[i] else f"band{i+1}"
                        for i in range(src.count)]
        log.info("  TC bands (%d): %s", src.count, descriptions)

        sigma0_idx = None
        for i, desc in enumerate(descriptions):
            dl = desc.lower()
            if "sigma" in dl or "vv" in dl:
                sigma0_idx = i + 1
        if sigma0_idx is None:
            sigma0_idx = 1

        sigma0 = src.read(sigma0_idx).astype(np.float32)
        transform = src.transform
        crs = src.crs

    pixel_size_m = abs(transform.a)
    log.info("  Pixel size: %.1f m, image: %dx%d", pixel_size_m, sigma0.shape[1], sigma0.shape[0])

    # Warp HAND + slope to match sigma0 grid
    tmp_hand = tc_tif.parent / f"_hand_warped_{tc_tif.stem}.tif"
    tmp_slope = tc_tif.parent / f"_slope_warped_{tc_tif.stem}.tif"
    warp_to_match(hand_tif, tc_tif, tmp_hand)
    warp_to_match(slope_tif, tc_tif, tmp_slope)

    with rasterio.open(tmp_hand) as src:
        hand = src.read(1).astype(np.float32)
    with rasterio.open(tmp_slope) as src:
        slope_deg = src.read(1).astype(np.float32)

    # Cleanup warped intermediates
    tmp_hand.unlink(missing_ok=True)
    tmp_slope.unlink(missing_ok=True)

    log.info("  HAND range: %.1f – %.1f m", np.nanmin(hand), np.nanmax(hand))
    log.info("  Slope range: %.1f – %.1f deg", np.nanmin(slope_deg), np.nanmax(slope_deg))

    # Compound mask via engine
    from engines.compound_mask import build_compound_mask
    mask, water_threshold = build_compound_mask(
        sigma0, hand, slope_deg,
        max_slope_deg=MAX_SLOPE_DEG,
        max_hand_m=MAX_HAND_M,
        threshold_min=WATER_THRESHOLD_MIN,
        threshold_max=WATER_THRESHOLD_MAX,
    )
    log.info("  Auto water threshold (Otsu): %.5f", water_threshold)

    n_valid = ((sigma0 > 0) & np.isfinite(sigma0)).sum()
    n_water = (mask == 1).sum()
    log.info("  Compound mask: %d water / %d valid (%.1f%%)",
             n_water, n_valid, 100 * n_water / max(n_valid, 1))

    # Write output
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": mask.shape[1],
        "height": mask.shape[0],
        "count": 1,
        "crs": crs,
        "transform": transform,
        "nodata": 255,
    }
    with rasterio.open(output_tif, "w", **profile) as dst:
        dst.write(mask, 1)

    log.info("  Wrote compound mask → %s", output_tif.name)

    # Remove TC intermediate
    if tc_tif.exists():
        tc_tif.unlink()
        log.info("  Removed intermediate %s", tc_tif.name)


# ---------------------------------------------------------------------------
# SNAP processing pipeline
# ---------------------------------------------------------------------------

def run_snap_pipeline(safe_path: Path, output_tif: Path, r: redis.Redis,
                      run_id: str | None = None):
    """Run full SAR → water mask pipeline via esa_snappy operators."""

    log.info("Step 1/8: Reading %s", safe_path.name)
    product = ProductIO.readProduct(str(safe_path))

    log.info("Step 2/8: ThermalNoiseRemoval (VV)")
    product = GPF.createProduct(
        "ThermalNoiseRemoval", _params(
            selectedPolarisations="VV",
            removeThermalNoise="true",
        ), product
    )

    log.info("Step 3/8: Apply-Orbit-File")
    product = GPF.createProduct(
        "Apply-Orbit-File", _params(
            orbitType="Sentinel Precise (Auto Download)",
            polyDegree="3",
            continueOnFail="true",
        ), product
    )

    log.info("Step 4/8: Subset to AOI")
    product = GPF.createProduct(
        "Subset", _params(
            geoRegion=SUBSET_WKT,
            copyMetadata="true",
        ), product
    )

    # Materialize subset to break lazy chain
    subset_tmp = output_tif.parent / f"_subset_tmp_{output_tif.stem}"
    log.info("  Materializing subset → %s.dim", subset_tmp.name)
    ProductIO.writeProduct(product, str(subset_tmp), "BEAM-DIMAP")
    product.dispose()
    product = ProductIO.readProduct(str(subset_tmp) + ".dim")
    log.info("  Subset OK (%d x %d)",
             product.getSceneRasterWidth(), product.getSceneRasterHeight())

    log.info("Step 5/8: Calibration (Sigma0)")
    product = GPF.createProduct(
        "Calibration", _params(
            outputSigmaBand="true",
            outputImageScaleInDb="false",
        ), product
    )

    log.info("Step 6/8: Speckle-Filter (Lee Sigma 3x3)")
    product = GPF.createProduct(
        "Speckle-Filter", _params(
            filter="Lee Sigma",
            filterSizeX="3",
            filterSizeY="3",
            estimateENL="true",
            numLooksStr="1",
            windowSize="7x7",
            targetWindowSizeStr="3x3",
            sigmaStr="0.9",
        ), product
    )

    log.info("Step 7/8: Terrain-Correction (SRTM 3Sec, %s, 10 m, saveDEM)", TC_EPSG)
    product = GPF.createProduct(
        "Terrain-Correction", _params(
            demName="SRTM 3Sec",
            imgResamplingMethod="NEAREST_NEIGHBOUR",
            demResamplingMethod="NEAREST_NEIGHBOUR",
            pixelSpacingInMeter="10.0",
            mapProjection=TC_EPSG,
            saveSelectedSourceBand="true",
            saveDEM="true",
        ), product
    )

    # Write TC result
    tc_tif = output_tif.parent / f"_tc_{output_tif.stem}.tif"
    log.info("  Writing TC GeoTIFF → %s", tc_tif.name)
    ProductIO.writeProduct(product, str(tc_tif), "GeoTIFF")
    product.dispose()

    # Emit SAR_COMPLETED event to control plane
    _emit_event(r, run_id, "SAR_COMPLETED", {
        "tc_tif": str(tc_tif),
    })

    log.info("Step 8/8: Compound mask (Otsu sigma0 AND slope<%d deg AND HAND<%dm)",
             MAX_SLOPE_DEG, MAX_HAND_M)
    create_compound_mask(tc_tif, output_tif, BASIN_GEOJSON, r, run_id)
    log.info("SNAP pipeline complete")


def clip_to_basin(input_tif: Path, output_tif: Path, geojson: Path):
    log.info("Clipping to basin + reprojecting to EPSG:4326")
    subprocess.run([
        "gdalwarp", "-overwrite",
        "-t_srs", "EPSG:4326",
        "-cutline", str(geojson),
        "-crop_to_cutline",
        "-dstalpha",
        str(input_tif), str(output_tif),
    ], check=True)
    log.info("Clipped result: %s", output_tif)


def _emit_event(r: redis.Redis, run_id: str | None, event: str, artifacts: dict):
    """Push event to control plane events queue.

    Delegates to shared.redis_helpers.emit_event if available.
    """
    if not run_id:
        return
    try:
        from shared.redis_helpers import emit_event
        emit_event(r, run_id, event, artifacts)
    except ImportError:
        r.rpush("cp_events", json.dumps({
            "run_id": run_id,
            "event": event,
            "artifacts": artifacts,
        }))


# ---------------------------------------------------------------------------
# Job handlers
# ---------------------------------------------------------------------------

def process_flood_job(payload: dict, r: redis.Redis):
    """Process a full flood pipeline job."""
    scene = payload["scene"]
    basin = payload.get("basin_geojson", str(BASIN_GEOJSON))
    run_id = payload.get("run_id")

    log.info("=== Flood job started: %s ===", scene)

    safe_path = unzip_if_needed(scene)
    scene_stem = safe_path.stem

    snap_tif = DATA_DIR / f"water_snap_{scene_stem}.tif"
    final_tif = DATA_DIR / f"water_mask_{scene_stem}.tif"
    basin_path = Path(basin) if os.path.isabs(basin) else BASIN_GEOJSON

    run_snap_pipeline(safe_path, snap_tif, r, run_id)
    clip_to_basin(snap_tif, final_tif, basin_path)

    # Cleanup intermediates
    if snap_tif.exists():
        snap_tif.unlink()
    for prefix in ("_subset_tmp_", "_tc_"):
        for f in DATA_DIR.glob(f"{prefix}water_snap_{scene_stem}*"):
            if f.is_file():
                f.unlink()
            elif f.is_dir():
                shutil.rmtree(f)

    log.info("=== Flood job done: %s → %s ===", scene, final_tif.name)

    _emit_event(r, run_id, "MASK_COMPLETED", {
        "water_mask_tif": str(final_tif),
        "scene": scene,
    })
    return str(final_tif)


def process_mask_job(payload: dict, r: redis.Redis):
    """Process a standalone mask job (HAND/slope already cached)."""
    tc_tif = Path(payload["tc_tif"])
    hand_tif = Path(payload["hand_tif"])
    slope_tif = Path(payload["slope_tif"])
    basin_geojson = Path(payload["basin_geojson"])
    output_tif = Path(payload["output_tif"])
    run_id = payload.get("run_id")

    log.info("=== Mask job started: %s ===", tc_tif.name)
    create_compound_mask(tc_tif, output_tif, basin_geojson, r, run_id)

    final_tif = output_tif.parent / f"water_mask_{output_tif.stem}.tif"
    clip_to_basin(output_tif, final_tif, basin_geojson)

    _emit_event(r, run_id, "MASK_COMPLETED", {
        "water_mask_tif": str(final_tif),
    })
    log.info("=== Mask job done → %s ===", final_tif.name)


# ---------------------------------------------------------------------------
# Main loop (listens on both queues)
# ---------------------------------------------------------------------------

def main():
    r = connect_redis()
    log.info("Worker ready, listening on 'flood_jobs' + 'mask_jobs' queues …")

    while True:
        try:
            # Listen on both queues with priority (flood_jobs first)
            job = r.blpop(["flood_jobs", "mask_jobs"], timeout=0)
            queue = job[0]
            payload = json.loads(job[1])

            if queue == "flood_jobs":
                r.set(f"job:{payload.get('scene', 'unknown')}:status", "processing")
                result = process_flood_job(payload, r)
                r.set(f"job:{payload.get('scene', 'unknown')}:status", "done")
                r.set(f"job:{payload.get('scene', 'unknown')}:result", result)
            elif queue == "mask_jobs":
                process_mask_job(payload, r)

        except Exception:
            log.exception("Job failed")
            try:
                scene = payload.get("scene", "unknown")
                r.set(f"job:{scene}:status", "error")
                _emit_event(r, payload.get("run_id"), "FAILED", {
                    "error": "Worker job failed",
                })
            except Exception:
                pass


if __name__ == "__main__":
    main()
