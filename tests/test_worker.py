"""Tests for worker/worker.py — Otsu threshold and compound mask logic.

These tests DON'T require SNAP/esa_snappy — only test pure Python functions.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

# We can't import the full worker.py (needs esa_snappy), so we test the
# pure functions by extracting them or duplicating the logic for unit testing.


class TestAutoDetectThreshold:
    """Test Otsu threshold on synthetic Sigma0 data.

    Reimplements the function locally to avoid esa_snappy import.
    """

    @staticmethod
    def auto_detect_threshold(sigma0, fallback=0.017, min_t=0.005, max_t=0.05):
        """Copy of worker.py auto_detect_threshold for testing."""
        valid = sigma0[np.isfinite(sigma0) & (sigma0 > 0)]
        if valid.size < 1000:
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
        threshold_linear = np.clip(threshold_linear, min_t, max_t)
        return float(threshold_linear)

    def test_bimodal_separates_water_and_land(self):
        """Two distinct populations → threshold between them."""
        np.random.seed(42)
        # Land: high backscatter (0.05 - 0.2)
        land = np.random.uniform(0.05, 0.2, 5000).astype(np.float32)
        # Water: low backscatter (0.002 - 0.01)
        water = np.random.uniform(0.002, 0.01, 1000).astype(np.float32)

        sigma0 = np.concatenate([land, water])
        threshold = self.auto_detect_threshold(sigma0)

        # Threshold should be between water and land peaks
        assert 0.005 <= threshold <= 0.05
        assert threshold > 0.008   # above water cluster
        assert threshold < 0.05    # below land cluster

    def test_clamped_to_range(self):
        """Threshold should be clamped to [0.005, 0.05]."""
        np.random.seed(42)
        # Very uniform data → Otsu might pick extreme value
        sigma0 = np.random.uniform(0.001, 0.002, 5000).astype(np.float32)
        threshold = self.auto_detect_threshold(sigma0)
        assert threshold >= 0.005 - 1e-9  # floating point tolerance
        assert threshold <= 0.05 + 1e-9

    def test_fallback_on_small_data(self):
        """Less than 1000 valid pixels → fallback threshold."""
        sigma0 = np.array([0.01, 0.02, 0.03], dtype=np.float32)
        threshold = self.auto_detect_threshold(sigma0)
        assert threshold == 0.017

    def test_handles_nan_and_zero(self):
        """NaN and zero values should be excluded."""
        np.random.seed(42)
        sigma0 = np.random.uniform(0.01, 0.1, 3000).astype(np.float32)
        sigma0[0:100] = 0
        sigma0[100:200] = np.nan

        threshold = self.auto_detect_threshold(sigma0)
        assert 0.005 <= threshold <= 0.05


class TestBasinHashWorker:
    """Test basin_hash function (duplicated in worker.py)."""

    @staticmethod
    def basin_hash(geojson_path):
        """Copy of worker.py basin_hash for testing."""
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

    def test_matches_dem_worker_hash(self, sample_basin_geojson):
        """Worker's basin_hash must produce same value as DEM worker's."""
        sys.path.insert(0, str(Path(__file__).parent.parent / "dem_worker"))
        from basin_utils import basin_hash as dem_hash

        worker_h = self.basin_hash(sample_basin_geojson)
        dem_h = dem_hash(sample_basin_geojson)
        assert worker_h == dem_h, "Worker and DEM worker hashes must match!"


class TestCompoundMaskLogic:
    """Test compound mask logic (σ⁰ < otsu AND slope < 8° AND HAND < 25m)."""

    def test_water_detected_in_valley(self):
        """Low backscatter + flat + low HAND → water."""
        sigma0 = np.array([[0.005]], dtype=np.float32)  # below typical threshold
        slope = np.array([[2.0]], dtype=np.float32)       # flat
        hand = np.array([[3.0]], dtype=np.float32)         # near stream
        threshold = 0.015

        valid = (sigma0 > 0) & np.isfinite(sigma0)
        water = valid & (sigma0 < threshold) & (slope < 8) & (hand < 25)
        assert water[0, 0] == True

    def test_shadow_rejected_by_slope(self):
        """Low backscatter on steep slope → NOT water (shadow)."""
        sigma0 = np.array([[0.005]], dtype=np.float32)
        slope = np.array([[30.0]], dtype=np.float32)  # steep!
        hand = np.array([[3.0]], dtype=np.float32)
        threshold = 0.015

        valid = (sigma0 > 0) & np.isfinite(sigma0)
        water = valid & (sigma0 < threshold) & (slope < 8) & (hand < 25)
        assert water[0, 0] == False

    def test_dark_pixel_rejected_by_hand(self):
        """Low backscatter but high above stream → NOT water."""
        sigma0 = np.array([[0.005]], dtype=np.float32)
        slope = np.array([[2.0]], dtype=np.float32)
        hand = np.array([[100.0]], dtype=np.float32)  # high above drainage
        threshold = 0.015

        valid = (sigma0 > 0) & np.isfinite(sigma0)
        water = valid & (sigma0 < threshold) & (slope < 8) & (hand < 25)
        assert water[0, 0] == False

    def test_bright_pixel_rejected_by_threshold(self):
        """High backscatter → NOT water (even in valley)."""
        sigma0 = np.array([[0.08]], dtype=np.float32)  # bright
        slope = np.array([[2.0]], dtype=np.float32)
        hand = np.array([[3.0]], dtype=np.float32)
        threshold = 0.015

        valid = (sigma0 > 0) & np.isfinite(sigma0)
        water = valid & (sigma0 < threshold) & (slope < 8) & (hand < 25)
        assert water[0, 0] == False

    def test_nodata_excluded(self):
        """Zero/NaN sigma0 → nodata (255), not water."""
        sigma0 = np.array([[0.0, np.nan]], dtype=np.float32)
        slope = np.array([[2.0, 2.0]], dtype=np.float32)
        hand = np.array([[3.0, 3.0]], dtype=np.float32)
        threshold = 0.015

        valid = (sigma0 > 0) & np.isfinite(sigma0)
        water = valid & (sigma0 < threshold) & (slope < 8) & (hand < 25)
        mask = np.where(valid, water.astype(np.uint8), 255)

        assert mask[0, 0] == 255  # zero → nodata
        assert mask[0, 1] == 255  # nan → nodata

    def test_realistic_proportions(self):
        """Compound mask should give ~1-10% water for realistic data."""
        np.random.seed(42)
        n = 10000

        # 90% land (bright, varied terrain)
        sigma0_land = np.random.uniform(0.03, 0.15, int(n * 0.9))
        slope_land = np.random.uniform(0, 30, int(n * 0.9))
        hand_land = np.random.uniform(0, 100, int(n * 0.9))

        # 10% water candidates (dark, flat, low HAND)
        sigma0_water = np.random.uniform(0.002, 0.012, int(n * 0.1))
        slope_water = np.random.uniform(0, 4, int(n * 0.1))
        hand_water = np.random.uniform(0, 10, int(n * 0.1))

        sigma0 = np.concatenate([sigma0_land, sigma0_water]).astype(np.float32)
        slope = np.concatenate([slope_land, slope_water]).astype(np.float32)
        hand = np.concatenate([hand_land, hand_water]).astype(np.float32)

        threshold = 0.015
        valid = (sigma0 > 0) & np.isfinite(sigma0)
        water = valid & (sigma0 < threshold) & (slope < 8) & (hand < 25)

        pct = water.sum() / valid.sum() * 100
        assert 1 <= pct <= 20, f"Water percentage {pct:.1f}% outside expected range"
