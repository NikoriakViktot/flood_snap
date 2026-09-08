"""Compound water mask: sigma0 + slope + HAND.

Pure numpy — no Redis, rasterio, or filesystem dependencies.
"""

import numpy as np

from .otsu import auto_detect_threshold

DEFAULT_MAX_SLOPE_DEG = 8.0
DEFAULT_MAX_HAND_M = 25.0


def build_compound_mask(
    sigma0: np.ndarray,
    hand: np.ndarray,
    slope_deg: np.ndarray,
    *,
    max_slope_deg: float = DEFAULT_MAX_SLOPE_DEG,
    max_hand_m: float = DEFAULT_MAX_HAND_M,
    water_threshold: float | None = None,
    threshold_min: float = 0.005,
    threshold_max: float = 0.05,
) -> tuple[np.ndarray, float]:
    """Build compound water mask from sigma0, HAND, and slope arrays.

    Args:
        sigma0: 2D backscatter (linear scale).
        hand: 2D HAND values (meters), same shape as sigma0.
        slope_deg: 2D slope values (degrees), same shape as sigma0.
        max_slope_deg: Maximum slope for water classification.
        max_hand_m: Maximum HAND for water classification.
        water_threshold: Explicit sigma0 threshold. If None, computed via Otsu.
        threshold_min: Lower clamp for Otsu threshold.
        threshold_max: Upper clamp for Otsu threshold.

    Returns:
        (mask, threshold): mask is uint8 (0=land, 1=water, 255=nodata),
        threshold is the sigma0 threshold used.
    """
    if water_threshold is None:
        water_threshold = auto_detect_threshold(
            sigma0,
            threshold_min=threshold_min,
            threshold_max=threshold_max,
        )

    valid = (sigma0 > 0) & np.isfinite(sigma0)
    water = valid & (sigma0 < water_threshold) & (slope_deg < max_slope_deg) & (hand < max_hand_m)
    mask = np.where(valid, water.astype(np.uint8), 255)

    return mask, water_threshold
