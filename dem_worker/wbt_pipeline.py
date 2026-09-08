"""
WhiteboxTools hydro pipeline: DEM → HAND + slope.

Steps:
  1. breach_depressions (fill_pits=True)
  2. d8_pointer
  3. d8_flow_accumulation (cells)
  4. Stream raster (acc >= threshold → 1, else 0)
  5. elevation_above_stream → HAND
  6. slope → degrees
  7. Cleanup intermediates
"""

import logging
import shutil
from pathlib import Path

import numpy as np
import rasterio
from whitebox import WhiteboxTools

log = logging.getLogger("wbt_pipeline")


def run_dem_pipeline(
    dem_tif: str | Path,
    output_dir: str | Path,
    stream_threshold: int = 1000,
) -> tuple[Path, Path]:
    """Run full DEM → HAND + slope pipeline.

    Args:
        dem_tif: Input single-band DEM GeoTIFF.
        output_dir: Directory for output files (hand.tif, slope.tif).
        stream_threshold: Flow accumulation cells to define streams.

    Returns:
        (hand_path, slope_path) — absolute Paths to output GeoTIFFs.
    """
    dem_tif = Path(dem_tif)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    tmp = output_dir / "_wbt_tmp"
    tmp.mkdir(exist_ok=True)

    hand_path = output_dir / "hand.tif"
    slope_path = output_dir / "slope.tif"

    # Intermediate files
    breached = str(tmp / "breached.tif")
    flow_dir = str(tmp / "flow_dir.tif")
    flow_acc = str(tmp / "flow_acc.tif")
    streams = str(tmp / "streams.tif")

    wbt = WhiteboxTools()
    wbt.set_verbose_mode(True)
    wbt.set_working_dir(str(tmp))

    # --- Step 1: Breach depressions ---
    log.info("WBT step 1/6: breach_depressions")
    ret = wbt.breach_depressions(str(dem_tif), breached, fill_pits=True)
    if ret != 0:
        raise RuntimeError(f"breach_depressions failed (code {ret})")

    # --- Step 2: D8 pointer ---
    log.info("WBT step 2/6: d8_pointer")
    ret = wbt.d8_pointer(breached, flow_dir)
    if ret != 0:
        raise RuntimeError(f"d8_pointer failed (code {ret})")

    # --- Step 3: D8 flow accumulation ---
    log.info("WBT step 3/6: d8_flow_accumulation")
    ret = wbt.d8_flow_accumulation(breached, flow_acc, out_type="cells")
    if ret != 0:
        raise RuntimeError(f"d8_flow_accumulation failed (code {ret})")

    # --- Step 4: Stream raster from accumulation threshold ---
    log.info("WBT step 4/6: stream extraction (threshold=%d cells)", stream_threshold)
    with rasterio.open(flow_acc) as src:
        acc_data = src.read(1)
        profile = src.profile.copy()

    stream_data = np.where(acc_data >= stream_threshold, 1, 0).astype(np.int32)
    n_stream = stream_data.sum()
    log.info("  Stream cells: %d / %d (%.2f%%)",
             n_stream, stream_data.size, 100 * n_stream / stream_data.size)

    if n_stream == 0:
        raise RuntimeError(
            f"No stream cells found with threshold={stream_threshold}. "
            "Try lowering stream_threshold."
        )

    profile.update(dtype="int32", nodata=-1)
    with rasterio.open(streams, "w", **profile) as dst:
        dst.write(stream_data, 1)

    # --- Step 5: HAND (elevation above stream) ---
    log.info("WBT step 5/6: elevation_above_stream → HAND")
    ret = wbt.elevation_above_stream(breached, streams, str(hand_path))
    if ret != 0:
        raise RuntimeError(f"elevation_above_stream failed (code {ret})")

    # --- Step 6: Slope ---
    log.info("WBT step 6/6: slope")
    ret = wbt.slope(breached, str(slope_path), zfactor=1.0)
    if ret != 0:
        raise RuntimeError(f"slope failed (code {ret})")

    # --- Verify outputs ---
    with rasterio.open(hand_path) as src:
        hand_data = src.read(1)
        valid = hand_data[np.isfinite(hand_data) & (hand_data >= 0)]
        log.info("  HAND range: %.1f – %.1f m (median %.1f)",
                 valid.min(), valid.max(), np.median(valid))

    with rasterio.open(slope_path) as src:
        slope_data = src.read(1)
        valid = slope_data[np.isfinite(slope_data) & (slope_data >= 0)]
        log.info("  Slope range: %.1f – %.1f° (median %.1f)",
                 valid.min(), valid.max(), np.median(valid))

    # --- Cleanup intermediates ---
    log.info("Cleaning up WBT intermediates")
    shutil.rmtree(tmp, ignore_errors=True)

    return hand_path, slope_path
