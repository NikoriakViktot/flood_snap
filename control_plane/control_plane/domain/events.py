"""
Events emitted by workers and consumed by the control plane.
"""

from enum import Enum


class EventType(str, Enum):
    CONTRACT_ACCEPTED = "CONTRACT_ACCEPTED"

    # SAR worker events
    SAR_COMPLETED = "SAR_COMPLETED"       # TC output written

    # DEM worker events
    DEM_COMPLETED = "DEM_COMPLETED"       # HAND + slope cached

    # SAR worker (mask phase) events
    MASK_COMPLETED = "MASK_COMPLETED"     # Water mask written

    # dem_analysis worker events (own bounded context)
    ANALYSIS_COMPLETED = "ANALYSIS_COMPLETED"  # all stems processed, manifest written
    ANALYSIS_FAILED    = "ANALYSIS_FAILED"      # pipeline error in analysis context

    # flood context failure
    FAILED = "FAILED"
