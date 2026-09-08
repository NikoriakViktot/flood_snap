"""Otsu threshold detection for SAR backscatter classification.

Pure numpy — no Redis, rasterio, or filesystem dependencies.
"""

import numpy as np

DEFAULT_FALLBACK = 0.017
DEFAULT_MIN = 0.005
DEFAULT_MAX = 0.05
MIN_VALID_PIXELS = 1000


def auto_detect_threshold(
    sigma0: np.ndarray,
    *,
    fallback: float = DEFAULT_FALLBACK,
    threshold_min: float = DEFAULT_MIN,
    threshold_max: float = DEFAULT_MAX,
    min_valid: int = MIN_VALID_PIXELS,
) -> float:
    """Otsu threshold on log10(sigma0), clamped to [threshold_min, threshold_max].

    Args:
        sigma0: 2D array of linear-scale backscatter values.
        fallback: Default threshold when too few valid pixels.
        threshold_min: Lower clamp for the result.
        threshold_max: Upper clamp for the result.
        min_valid: Minimum number of valid pixels required for Otsu.

    Returns:
        Water/non-water threshold in linear sigma0 units.
    """
    valid = sigma0[np.isfinite(sigma0) & (sigma0 > 0)]
    if valid.size < min_valid:
        return fallback

    data = np.log10(valid)
    hist, bin_edges = np.histogram(data, bins=256)
    hist = hist.astype(np.float64)

    prob = hist / hist.sum()
    omega = np.cumsum(prob)
    mu = np.cumsum(prob * bin_edges[:-1])
    mu_t = mu[-1]

    sigma_b = (mu_t * omega - mu) ** 2 / (omega * (1 - omega) + 1e-10)
    idx = np.argmax(sigma_b)
    threshold_log = bin_edges[idx]
    threshold_linear = 10 ** threshold_log

    threshold_linear = np.clip(threshold_linear, threshold_min, threshold_max)
    return float(threshold_linear)
