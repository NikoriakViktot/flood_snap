"""Tests for dem_worker/wbt_pipeline.py — requires WhiteboxTools binary."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import rasterio

# Mock the whitebox module before importing wbt_pipeline
mock_whitebox = MagicMock()
sys.modules["whitebox"] = mock_whitebox

sys.path.insert(0, str(Path(__file__).parent.parent / "dem_worker"))


class TestWbtPipelineMocked:
    """Test pipeline logic with mocked WhiteboxTools (no binary needed)."""

    @patch("wbt_pipeline.WhiteboxTools")
    def test_pipeline_creates_output_files(self, mock_wbt_cls, sample_dem_tif, tmp_path):
        """Pipeline should create hand.tif and slope.tif."""
        from wbt_pipeline import run_dem_pipeline

        wbt = MagicMock()
        mock_wbt_cls.return_value = wbt

        # Mock WBT operations to return 0 (success) and create output files
        def mock_breach(inp, out, **kwargs):
            # Copy input to output
            import shutil
            shutil.copy(inp, out)
            return 0

        def mock_d8_pointer(inp, out, **kwargs):
            import shutil
            shutil.copy(inp, out)
            return 0

        def mock_d8_flow_acc(inp, out, **kwargs):
            # Create flow accumulation with some cells > threshold
            with rasterio.open(inp) as src:
                profile = src.profile.copy()
                data = np.random.randint(0, 2000, (src.height, src.width), dtype=np.int32)
            profile.update(dtype="int32")
            with rasterio.open(out, "w", **profile) as dst:
                dst.write(data, 1)
            return 0

        def mock_elevation_above_stream(dem, streams, out, **kwargs):
            with rasterio.open(dem) as src:
                profile = src.profile.copy()
                # Synthetic HAND: 0-50m
                hand = np.random.uniform(0, 50, (src.height, src.width)).astype(np.float32)
            with rasterio.open(out, "w", **profile) as dst:
                dst.write(hand, 1)
            return 0

        def mock_slope(inp, out, **kwargs):
            with rasterio.open(inp) as src:
                profile = src.profile.copy()
                slope = np.random.uniform(0, 30, (src.height, src.width)).astype(np.float32)
            with rasterio.open(out, "w", **profile) as dst:
                dst.write(slope, 1)
            return 0

        wbt.breach_depressions.side_effect = mock_breach
        wbt.d8_pointer.side_effect = mock_d8_pointer
        wbt.d8_flow_accumulation.side_effect = mock_d8_flow_acc
        wbt.elevation_above_stream.side_effect = mock_elevation_above_stream
        wbt.slope.side_effect = mock_slope

        out_dir = tmp_path / "output"
        hand_path, slope_path = run_dem_pipeline(sample_dem_tif, out_dir, stream_threshold=500)

        assert hand_path.exists()
        assert slope_path.exists()
        assert hand_path.name == "hand.tif"
        assert slope_path.name == "slope.tif"

        # Verify HAND values
        with rasterio.open(hand_path) as src:
            hand = src.read(1)
            valid = hand[np.isfinite(hand) & (hand >= 0)]
            assert valid.min() >= 0
            assert valid.max() <= 50

        # Verify slope values
        with rasterio.open(slope_path) as src:
            slope = src.read(1)
            valid = slope[np.isfinite(slope) & (slope >= 0)]
            assert valid.min() >= 0
            assert valid.max() <= 30

    @patch("wbt_pipeline.WhiteboxTools")
    def test_pipeline_cleans_intermediates(self, mock_wbt_cls, sample_dem_tif, tmp_path):
        """Intermediate _wbt_tmp directory should be cleaned up."""
        from wbt_pipeline import run_dem_pipeline

        wbt = MagicMock()
        mock_wbt_cls.return_value = wbt

        # Simple mocks that create output files
        def mock_op(*args, **kw):
            import shutil
            # Last positional arg is output, first is input
            inp, out = args[0], args[-1]
            if isinstance(inp, str) and Path(inp).exists():
                shutil.copy(inp, out)
            else:
                with rasterio.open(sample_dem_tif) as src:
                    profile = src.profile.copy()
                    data = np.random.uniform(0, 50, (src.height, src.width)).astype(np.float32)
                with rasterio.open(out, "w", **profile) as dst:
                    dst.write(data, 1)
            return 0

        def mock_flow_acc(inp, out, **kw):
            with rasterio.open(inp) as src:
                profile = src.profile.copy()
                data = np.random.randint(0, 2000, (src.height, src.width), dtype=np.int32)
            profile.update(dtype="int32")
            with rasterio.open(out, "w", **profile) as dst:
                dst.write(data, 1)
            return 0

        wbt.breach_depressions.side_effect = mock_op
        wbt.d8_pointer.side_effect = mock_op
        wbt.d8_flow_accumulation.side_effect = mock_flow_acc
        wbt.elevation_above_stream.side_effect = mock_op
        wbt.slope.side_effect = mock_op

        out_dir = tmp_path / "output"
        run_dem_pipeline(sample_dem_tif, out_dir, stream_threshold=500)

        # _wbt_tmp should be cleaned up
        assert not (out_dir / "_wbt_tmp").exists()

    @patch("wbt_pipeline.WhiteboxTools")
    def test_pipeline_fails_on_breach_error(self, mock_wbt_cls, sample_dem_tif, tmp_path):
        """Non-zero return from WBT should raise RuntimeError."""
        from wbt_pipeline import run_dem_pipeline

        wbt = MagicMock()
        mock_wbt_cls.return_value = wbt
        wbt.breach_depressions.return_value = 1  # failure

        with pytest.raises(RuntimeError, match="breach_depressions failed"):
            run_dem_pipeline(sample_dem_tif, tmp_path / "output")

    @patch("wbt_pipeline.WhiteboxTools")
    def test_pipeline_fails_on_zero_streams(self, mock_wbt_cls, sample_dem_tif, tmp_path):
        """Zero stream cells should raise RuntimeError."""
        from wbt_pipeline import run_dem_pipeline

        wbt = MagicMock()
        mock_wbt_cls.return_value = wbt

        def mock_breach(inp, out, **kw):
            import shutil
            shutil.copy(inp, out)
            return 0

        def mock_d8(inp, out, **kw):
            import shutil
            shutil.copy(inp, out)
            return 0

        def mock_flow_acc_zero(inp, out, **kw):
            # All zeros — no cells above threshold
            with rasterio.open(inp) as src:
                profile = src.profile.copy()
                data = np.zeros((src.height, src.width), dtype=np.int32)
            profile.update(dtype="int32")
            with rasterio.open(out, "w", **profile) as dst:
                dst.write(data, 1)
            return 0

        wbt.breach_depressions.side_effect = mock_breach
        wbt.d8_pointer.side_effect = mock_d8
        wbt.d8_flow_accumulation.side_effect = mock_flow_acc_zero

        with pytest.raises(RuntimeError, match="No stream cells found"):
            run_dem_pipeline(sample_dem_tif, tmp_path / "output", stream_threshold=1000)
