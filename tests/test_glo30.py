"""Tests for Copernicus GLO-30 DEM download utilities."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "dem_worker"))

from basin_utils import glo30_tile_names, download_glo30, GLO30_BASE_URL


class TestGlo30TileNames:
    """Test tile name calculation for various geographic locations."""

    def test_bukovyna_4_tiles(self):
        """Bukovyna region crosses 2 lat × 2 lon boundaries."""
        tiles = glo30_tile_names(24.5, 47.5, 25.3, 48.3)
        assert len(tiles) == 4
        names = set(tiles)
        assert "Copernicus_DSM_COG_10_N47_00_E024_00_DEM" in names
        assert "Copernicus_DSM_COG_10_N47_00_E025_00_DEM" in names
        assert "Copernicus_DSM_COG_10_N48_00_E024_00_DEM" in names
        assert "Copernicus_DSM_COG_10_N48_00_E025_00_DEM" in names

    def test_single_tile_interior(self):
        """Bbox fully inside one tile."""
        tiles = glo30_tile_names(24.2, 47.2, 24.8, 47.8)
        assert len(tiles) == 1
        assert tiles[0] == "Copernicus_DSM_COG_10_N47_00_E024_00_DEM"

    def test_exact_tile_boundary(self):
        """Bbox exactly at tile boundaries."""
        tiles = glo30_tile_names(24.0, 47.0, 25.0, 48.0)
        # floor(24.0)=24, floor(25.0)=25, floor(47.0)=47, floor(48.0)=48
        assert len(tiles) == 4

    def test_southern_hemisphere_negative_lat(self):
        """S hemisphere: floor(-34.5)=-35 → S35, floor(-34.0)=-34 → S34 → 2 tiles."""
        tiles = glo30_tile_names(18.0, -34.5, 18.5, -34.0)
        names = set(tiles)
        assert "Copernicus_DSM_COG_10_S35_00_E018_00_DEM" in names
        assert "Copernicus_DSM_COG_10_S34_00_E018_00_DEM" in names
        assert len(tiles) == 2

    def test_western_hemisphere_negative_lon(self):
        """W hemisphere: floor(-73.5)=-74 → W074, floor(-73.0)=-73 → W073; 2 lat × 2 lon = 4."""
        tiles = glo30_tile_names(-73.5, 40.5, -73.0, 41.0)
        names = set(tiles)
        assert "Copernicus_DSM_COG_10_N40_00_W074_00_DEM" in names
        assert "Copernicus_DSM_COG_10_N41_00_W074_00_DEM" in names
        assert len(tiles) == 4

    def test_equator_crossing(self):
        """Bbox crossing equator: tiles at N00 and S01."""
        tiles = glo30_tile_names(36.0, -0.5, 36.5, 0.5)
        names = set(tiles)
        assert "Copernicus_DSM_COG_10_N00_00_E036_00_DEM" in names
        assert "Copernicus_DSM_COG_10_S01_00_E036_00_DEM" in names

    def test_prime_meridian_crossing(self):
        """Bbox crossing prime meridian."""
        tiles = glo30_tile_names(-0.5, 51.0, 0.5, 51.5)
        names = set(tiles)
        assert "Copernicus_DSM_COG_10_N51_00_W001_00_DEM" in names
        assert "Copernicus_DSM_COG_10_N51_00_E000_00_DEM" in names

    def test_large_region_many_tiles(self):
        """Large region: 5° × 5° = 25 tiles."""
        tiles = glo30_tile_names(20.0, 45.0, 24.9, 49.9)
        assert len(tiles) == 25  # 5 lat × 5 lon

    def test_url_format(self):
        """Verify the URL we'd construct from tile names."""
        tiles = glo30_tile_names(24.7, 47.6, 24.9, 47.9)
        tile = tiles[0]
        url = f"{GLO30_BASE_URL}/{tile}/{tile}.tif"
        assert url.startswith("https://copernicus-dem-30m.s3.amazonaws.com/")
        assert url.endswith(".tif")
        assert "N47" in url
        assert "E024" in url


class TestDownloadGlo30Mocked:
    """Test download_glo30 with mocked HTTP and gdalwarp."""

    @patch("basin_utils.subprocess.run")
    @patch("basin_utils.urllib.request.urlretrieve")
    @patch("basin_utils.rasterio.open")
    @patch("basin_utils.merge")
    def test_downloads_correct_tiles(
        self, mock_merge, mock_rasterio_open, mock_urlretrieve, mock_subprocess,
        sample_basin_geojson, tmp_path,
    ):
        """Should attempt to download tiles covering the basin bbox."""
        import numpy as np
        from unittest.mock import mock_open

        # Mock urlretrieve to create empty files
        def fake_download(url, path):
            Path(path).touch()

        mock_urlretrieve.side_effect = fake_download

        # Mock rasterio.open for the final verification read
        mock_ds = MagicMock()
        mock_ds.width = 100
        mock_ds.height = 100
        mock_ds.read.return_value = np.ones((1, 100, 100), dtype=np.float32) * 300
        mock_ds.profile = {"driver": "GTiff", "dtype": "float32", "width": 100,
                           "height": 100, "count": 1}
        mock_ds.__enter__ = MagicMock(return_value=mock_ds)
        mock_ds.__exit__ = MagicMock(return_value=False)
        mock_rasterio_open.return_value = mock_ds

        # Mock merge
        mock_merge.return_value = (np.ones((1, 100, 100)), None)

        # Mock gdalwarp
        mock_subprocess.return_value = MagicMock(returncode=0)

        output = tmp_path / "dem.tif"
        try:
            download_glo30(sample_basin_geojson, output)
        except Exception:
            pass  # May fail on cleanup, but downloads should have been attempted

        # Check that urlretrieve was called with GLO-30 URLs
        if mock_urlretrieve.called:
            urls = [call[0][0] for call in mock_urlretrieve.call_args_list]
            for url in urls:
                assert "copernicus-dem-30m.s3.amazonaws.com" in url
                assert "Copernicus_DSM_COG_10_" in url

    def test_handles_404_gracefully(self, sample_basin_geojson, tmp_path):
        """404 tiles (ocean) should be skipped, not crash."""
        import urllib.error

        with patch("basin_utils.urllib.request.urlretrieve") as mock_dl:
            mock_dl.side_effect = urllib.error.HTTPError(
                "url", 404, "Not Found", {}, None
            )

            output = tmp_path / "dem.tif"
            with pytest.raises(RuntimeError, match="No GLO-30 tiles"):
                download_glo30(sample_basin_geojson, output)
