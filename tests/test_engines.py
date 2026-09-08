"""Tests for worker engines — pure numpy computation."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "worker"))

from engines.otsu import auto_detect_threshold
from engines.compound_mask import build_compound_mask


class TestAutoDetectThreshold:
    def test_returns_fallback_with_few_pixels(self):
        small = np.array([0.01, 0.02, 0.03])
        result = auto_detect_threshold(small, min_valid=1000)
        assert result == 0.017

    def test_returns_clamped_threshold(self):
        np.random.seed(42)
        # Mix of "water" and "land" pixels
        water = np.random.uniform(0.003, 0.01, 5000).astype(np.float32)
        land = np.random.uniform(0.03, 0.1, 5000).astype(np.float32)
        sigma0 = np.concatenate([water, land])
        result = auto_detect_threshold(sigma0)
        assert 0.005 <= result <= 0.05

    def test_handles_nan_and_zero(self):
        np.random.seed(42)
        data = np.random.uniform(0.01, 0.1, 5000).astype(np.float32)
        data[:100] = 0  # zeros
        data[100:200] = np.nan  # NaN
        result = auto_detect_threshold(data)
        assert 0.005 <= result <= 0.05

    def test_all_invalid_returns_fallback(self):
        data = np.zeros(100, dtype=np.float32)
        assert auto_detect_threshold(data) == 0.017

    def test_custom_bounds(self):
        np.random.seed(42)
        water = np.random.uniform(0.001, 0.005, 5000).astype(np.float32)
        land = np.random.uniform(0.05, 0.1, 5000).astype(np.float32)
        sigma0 = np.concatenate([water, land])
        result = auto_detect_threshold(sigma0, threshold_min=0.01, threshold_max=0.02)
        assert abs(result - np.clip(result, 0.01, 0.02)) < 1e-6


class TestBuildCompoundMask:
    @pytest.fixture
    def sample_arrays(self):
        np.random.seed(42)
        h, w = 100, 100
        sigma0 = np.random.uniform(0.02, 0.1, (h, w)).astype(np.float32)
        sigma0[40:60, 40:60] = np.random.uniform(0.003, 0.01, (20, 20))  # water
        hand = np.random.uniform(0, 50, (h, w)).astype(np.float32)
        hand[40:60, 40:60] = np.random.uniform(0, 5, (20, 20))  # low HAND in water area
        slope = np.random.uniform(0, 30, (h, w)).astype(np.float32)
        slope[40:60, 40:60] = np.random.uniform(0, 3, (20, 20))  # low slope in water
        return sigma0, hand, slope

    def test_output_shape(self, sample_arrays):
        sigma0, hand, slope = sample_arrays
        mask, threshold = build_compound_mask(sigma0, hand, slope)
        assert mask.shape == sigma0.shape
        assert mask.dtype == np.uint8

    def test_water_detected(self, sample_arrays):
        sigma0, hand, slope = sample_arrays
        mask, threshold = build_compound_mask(sigma0, hand, slope)
        water_count = (mask == 1).sum()
        assert water_count > 0

    def test_nodata_for_invalid(self):
        sigma0 = np.zeros((10, 10), dtype=np.float32)  # all invalid (0)
        hand = np.ones((10, 10), dtype=np.float32)
        slope = np.ones((10, 10), dtype=np.float32)
        mask, _ = build_compound_mask(sigma0, hand, slope)
        assert (mask == 255).all()

    def test_explicit_threshold(self, sample_arrays):
        sigma0, hand, slope = sample_arrays
        mask1, t1 = build_compound_mask(sigma0, hand, slope, water_threshold=0.01)
        mask2, t2 = build_compound_mask(sigma0, hand, slope, water_threshold=0.05)
        assert t1 == 0.01
        assert t2 == 0.05
        # More generous threshold should find more water
        assert (mask2 == 1).sum() >= (mask1 == 1).sum()

    def test_slope_constraint(self, sample_arrays):
        sigma0, hand, slope = sample_arrays
        # Very low slope threshold should reduce water detection
        mask_strict, _ = build_compound_mask(sigma0, hand, slope, max_slope_deg=1.0)
        mask_relaxed, _ = build_compound_mask(sigma0, hand, slope, max_slope_deg=90.0)
        assert (mask_relaxed == 1).sum() >= (mask_strict == 1).sum()

    def test_hand_constraint(self, sample_arrays):
        sigma0, hand, slope = sample_arrays
        mask_strict, _ = build_compound_mask(sigma0, hand, slope, max_hand_m=1.0)
        mask_relaxed, _ = build_compound_mask(sigma0, hand, slope, max_hand_m=1000.0)
        assert (mask_relaxed == 1).sum() >= (mask_strict == 1).sum()
