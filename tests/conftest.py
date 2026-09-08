"""
Shared test fixtures for flood_snap tests.
"""

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds


@pytest.fixture
def tmp_dir(tmp_path):
    """Temporary directory for test outputs."""
    return tmp_path


@pytest.fixture
def sample_basin_geojson(tmp_path) -> Path:
    """Create a minimal basin GeoJSON for testing."""
    geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"name": "test_basin"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [24.7, 47.6],
                    [25.2, 47.6],
                    [25.2, 48.2],
                    [24.7, 48.2],
                    [24.7, 47.6],
                ]]
            }
        }]
    }
    path = tmp_path / "basin.geojson"
    path.write_text(json.dumps(geojson))
    return path


@pytest.fixture
def sample_dem_tif(tmp_path) -> Path:
    """Create a synthetic DEM GeoTIFF for testing (100x100, UTM)."""
    path = tmp_path / "dem.tif"
    height, width = 100, 100

    # Synthetic terrain: gentle slope with a valley
    x = np.linspace(0, 1, width)
    y = np.linspace(0, 1, height)
    xx, yy = np.meshgrid(x, y)

    # Valley running diagonally, elevation 100-500m
    elevation = 200 + 300 * (np.abs(xx - 0.5) + np.abs(yy - 0.5))
    elevation = elevation.astype(np.float32)

    # UTM-like transform (10m pixels, arbitrary origin)
    transform = from_bounds(500000, 5270000, 501000, 5271000, width, height)

    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:32635",
        "transform": transform,
        "nodata": -9999,
    }

    with rasterio.open(path, "w", **profile) as dst:
        dst.write(elevation, 1)

    return path


@pytest.fixture
def sample_tc_tif(tmp_path) -> Path:
    """Create a synthetic SNAP TC output (2-band: Sigma0 + elevation)."""
    path = tmp_path / "tc_output.tif"
    height, width = 100, 100

    # Band 1: Sigma0_VV (linear, with some "water" pixels)
    sigma0 = np.random.uniform(0.02, 0.1, (height, width)).astype(np.float32)
    # Add water: low backscatter region
    sigma0[40:60, 40:60] = np.random.uniform(0.003, 0.01, (20, 20))

    # Band 2: elevation
    x = np.linspace(0, 1, width)
    y = np.linspace(0, 1, height)
    xx, yy = np.meshgrid(x, y)
    elevation = (200 + 300 * (np.abs(xx - 0.5) + np.abs(yy - 0.5))).astype(np.float32)

    transform = from_bounds(500000, 5270000, 501000, 5271000, width, height)

    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": width,
        "height": height,
        "count": 2,
        "crs": "EPSG:32635",
        "transform": transform,
    }

    with rasterio.open(path, "w", **profile) as dst:
        dst.write(sigma0, 1)
        dst.write(elevation, 2)
        dst.set_band_description(1, "Sigma0_VV")
        dst.set_band_description(2, "elevation")

    return path


@pytest.fixture
def sample_sigma0(tmp_path) -> tuple[Path, np.ndarray]:
    """Create a standalone Sigma0 GeoTIFF and return (path, data)."""
    path = tmp_path / "sigma0.tif"
    height, width = 100, 100

    sigma0 = np.random.uniform(0.02, 0.1, (height, width)).astype(np.float32)
    sigma0[40:60, 40:60] = np.random.uniform(0.003, 0.01, (20, 20))

    transform = from_bounds(500000, 5270000, 501000, 5271000, width, height)

    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:32635",
        "transform": transform,
    }

    with rasterio.open(path, "w", **profile) as dst:
        dst.write(sigma0, 1)

    return path, sigma0
