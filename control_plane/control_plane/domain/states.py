"""
Run states for flood/hydro processing pipeline.

Pipeline flow:
  CREATED → SAR_RUNNING → SAR_READY → DEM_RUNNING → DEM_READY
    → MASK_RUNNING → DONE

Any state + FAILED → FAILED
"""

from enum import Enum


class RunState(str, Enum):
    CREATED = "CREATED"

    # SAR pre-processing (SNAP: noise removal, orbit, calibration, TC)
    SAR_RUNNING = "SAR_RUNNING"
    SAR_READY = "SAR_READY"       # TC output available (Sigma0 + elevation)

    # DEM derivatives (WhiteboxTools: HAND + slope)
    DEM_RUNNING = "DEM_RUNNING"
    DEM_READY = "DEM_READY"       # HAND + slope cached

    # Water mask (compound: Otsu + slope + HAND)
    MASK_RUNNING = "MASK_RUNNING"

    # dem_analysis bounded context — own lifecycle, own terminal states
    ANALYSIS_RUNNING   = "ANALYSIS_RUNNING"
    ANALYSIS_COMPLETED = "ANALYSIS_COMPLETED"   # terminal (success)
    ANALYSIS_FAILED    = "ANALYSIS_FAILED"       # terminal (failure)

    # basin delineation bounded context (click → watershed → passport)
    BASIN_RUNNING = "BASIN_RUNNING"

    # flood context terminals
    DONE   = "DONE"
    FAILED = "FAILED"
