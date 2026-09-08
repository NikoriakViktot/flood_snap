"""Basin geometry utilities — shared between workers.

Canonical implementations of basin_hash, basin_bbox, geometry extraction.
"""

import hashlib
import json
from pathlib import Path


def _extract_geometry(data: dict) -> dict:
    """Extract geometry from GeoJSON (Feature, FeatureCollection, or bare)."""
    if data.get("type") == "FeatureCollection":
        return data["features"][0]["geometry"]
    elif data.get("type") == "Feature":
        return data["geometry"]
    return data


def _flatten_coords(geom: dict) -> list[list[float]]:
    """Recursively flatten all coordinates from a GeoJSON geometry."""
    coords = geom.get("coordinates", [])
    result = []

    def _recurse(item):
        if isinstance(item, (list, tuple)):
            if len(item) >= 2 and isinstance(item[0], (int, float)):
                result.append(item)
            else:
                for sub in item:
                    _recurse(sub)

    _recurse(coords)
    return result


def basin_hash(geojson_path: str | Path) -> str:
    """Compute 12-char SHA256 of basin geometry for cache key.

    Deterministic: same geometry -> same hash.
    """
    with open(geojson_path) as f:
        data = json.load(f)

    geom = _extract_geometry(data)
    raw = json.dumps(geom, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def basin_bbox(geojson_path: str | Path) -> tuple[float, float, float, float]:
    """Extract bounding box (west, south, east, north) from GeoJSON."""
    with open(geojson_path) as f:
        data = json.load(f)

    geom = _extract_geometry(data)
    coords = _flatten_coords(geom)

    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]

    return (min(lons), min(lats), max(lons), max(lats))
