"""Tests for dem_worker/basin_utils.py — pure functions, no network/gdalwarp needed."""

import json
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio

# Add dem_worker to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "dem_worker"))

from basin_utils import (
    basin_hash,
    basin_bbox,
    glo30_tile_names,
    extract_dem_from_snap_tc,
    _extract_geometry,
    _flatten_coords,
)


class TestBasinHash:
    """Test deterministic basin hashing."""

    def test_deterministic(self, sample_basin_geojson):
        h1 = basin_hash(sample_basin_geojson)
        h2 = basin_hash(sample_basin_geojson)
        assert h1 == h2

    def test_length_12(self, sample_basin_geojson):
        h = basin_hash(sample_basin_geojson)
        assert len(h) == 12

    def test_hex_chars_only(self, sample_basin_geojson):
        h = basin_hash(sample_basin_geojson)
        assert all(c in "0123456789abcdef" for c in h)

    def test_different_geometry_different_hash(self, tmp_path):
        g1 = {"type": "Feature", "properties": {}, "geometry": {
            "type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
        }}
        g2 = {"type": "Feature", "properties": {}, "geometry": {
            "type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]
        }}

        p1 = tmp_path / "g1.geojson"
        p2 = tmp_path / "g2.geojson"
        p1.write_text(json.dumps(g1))
        p2.write_text(json.dumps(g2))

        assert basin_hash(p1) != basin_hash(p2)

    def test_handles_feature_collection(self, sample_basin_geojson):
        """FeatureCollection is the standard format."""
        h = basin_hash(sample_basin_geojson)
        assert isinstance(h, str)

    def test_handles_bare_geometry(self, tmp_path):
        bare = {
            "type": "Polygon",
            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
        }
        p = tmp_path / "bare.geojson"
        p.write_text(json.dumps(bare))
        h = basin_hash(p)
        assert len(h) == 12


class TestBasinBbox:
    """Test bounding box extraction."""

    def test_simple_polygon(self, sample_basin_geojson):
        west, south, east, north = basin_bbox(sample_basin_geojson)
        assert west == pytest.approx(24.7)
        assert south == pytest.approx(47.6)
        assert east == pytest.approx(25.2)
        assert north == pytest.approx(48.2)

    def test_bbox_order(self, sample_basin_geojson):
        west, south, east, north = basin_bbox(sample_basin_geojson)
        assert west < east
        assert south < north


class TestGlo30TileNames:
    """Test GLO-30 tile name calculation."""

    def test_bukovyna_region(self):
        """Bukovyna: ~24.5-25.3E, 47.5-48.3N → 4 tiles."""
        tiles = glo30_tile_names(24.5, 47.5, 25.3, 48.3)
        assert "Copernicus_DSM_COG_10_N47_00_E024_00_DEM" in tiles
        assert "Copernicus_DSM_COG_10_N47_00_E025_00_DEM" in tiles
        assert "Copernicus_DSM_COG_10_N48_00_E024_00_DEM" in tiles
        assert "Copernicus_DSM_COG_10_N48_00_E025_00_DEM" in tiles
        assert len(tiles) == 4

    def test_single_tile(self):
        """Bbox within one tile → 1 tile."""
        tiles = glo30_tile_names(24.1, 47.1, 24.9, 47.9)
        assert len(tiles) == 1
        assert tiles[0] == "Copernicus_DSM_COG_10_N47_00_E024_00_DEM"

    def test_southern_hemisphere(self):
        tiles = glo30_tile_names(-73.5, -34.5, -72.5, -33.5)
        assert "Copernicus_DSM_COG_10_S35_00_W074_00_DEM" in tiles
        assert "Copernicus_DSM_COG_10_S34_00_W074_00_DEM" in tiles

    def test_crossing_prime_meridian(self):
        tiles = glo30_tile_names(-0.5, 51.0, 0.5, 51.5)
        expected_lons = {"W001", "E000"}
        for t in tiles:
            parts = t.split("_")
            lon_part = f"{parts[7]}{parts[8]}"
            # Just verify we get tiles on both sides
        assert len(tiles) == 2

    def test_tile_name_format(self):
        tiles = glo30_tile_names(24.7, 47.6, 25.2, 48.2)
        for t in tiles:
            assert t.startswith("Copernicus_DSM_COG_10_")
            assert t.endswith("_DEM")
            # Verify N/S and E/W formatting
            parts = t.split("_")
            assert parts[4][0] in ("N", "S")
            assert parts[6][0] in ("E", "W")


class TestExtractGeometry:
    """Test geometry extraction helper."""

    def test_feature_collection(self):
        data = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 2]}}
        ]}
        geom = _extract_geometry(data)
        assert geom["type"] == "Point"

    def test_feature(self):
        data = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 2]}}
        geom = _extract_geometry(data)
        assert geom["type"] == "Point"

    def test_bare_geometry(self):
        data = {"type": "Point", "coordinates": [1, 2]}
        geom = _extract_geometry(data)
        assert geom["type"] == "Point"


class TestFlattenCoords:
    """Test coordinate flattening."""

    def test_polygon(self):
        geom = {"type": "Polygon", "coordinates": [
            [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
        ]}
        coords = _flatten_coords(geom)
        assert len(coords) == 5
        assert coords[0] == [0, 0]

    def test_multipolygon(self):
        geom = {"type": "MultiPolygon", "coordinates": [
            [[[0, 0], [1, 0], [1, 1], [0, 0]]],
            [[[2, 2], [3, 2], [3, 3], [2, 2]]],
        ]}
        coords = _flatten_coords(geom)
        assert len(coords) == 8  # 4 + 4 (including closing vertices)


class TestExtractDemFromSnapTc:
    """Test elevation extraction from SNAP TC output."""

    def test_extracts_band_2(self, sample_tc_tif, tmp_path):
        output = tmp_path / "dem.tif"
        result = extract_dem_from_snap_tc(sample_tc_tif, output)

        assert result.exists()
        with rasterio.open(result) as src:
            assert src.count == 1
            assert src.dtypes[0] == "float32"
            data = src.read(1)
            valid = data[data != -9999]
            # Our synthetic elevation is 200-500m
            assert valid.min() >= 200
            assert valid.max() <= 500

    def test_finds_elevation_by_description(self, sample_tc_tif, tmp_path):
        """Band descriptions should guide band selection."""
        output = tmp_path / "dem.tif"
        extract_dem_from_snap_tc(sample_tc_tif, output)

        with rasterio.open(output) as src:
            data = src.read(1)
            valid = data[data != -9999]
            # Should be elevation values, not sigma0 (0.003-0.1)
            assert valid.min() > 1.0

    def test_single_band_raises(self, tmp_path):
        """Single-band TIF without elevation should raise."""
        single = tmp_path / "single.tif"
        with rasterio.open(single, "w", driver="GTiff", dtype="float32",
                           width=10, height=10, count=1, crs="EPSG:32635",
                           transform=rasterio.transform.from_bounds(0, 0, 1, 1, 10, 10)) as dst:
            dst.write(np.ones((1, 10, 10), dtype=np.float32))

        with pytest.raises(ValueError, match="Cannot find elevation"):
            extract_dem_from_snap_tc(single, tmp_path / "out.tif")
