"""Provenance of a HAND model: software versions, the hashed configuration and the metadata package."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess

from lab_service import settings

METHOD_REFS = [
    {"ref": "Rennó et al. (2008) HAND, a new terrain descriptor using SRTM-DEM", "doi": "10.1016/j.rse.2008.03.018"},
    {"ref": "Nobre et al. (2011) Height Above the Nearest Drainage", "doi": "10.1016/j.jhydrol.2011.03.051"},
    {"ref": "NOAA OWP inundation-mapping (FIM v4), method reference", "url": "https://github.com/NOAA-OWP/inundation-mapping"},
]
LIMITATIONS = [
    "HAND is a terrain descriptor (height above the nearest drainage along the D8 flow path), not a flood depth",
    "no hydraulic routing: a water level applied to HAND is an exploratory scenario, not a hydrodynamic model",
    "gauge datums (BS-77 / EVRF2019) are not resolved against the DEM's EGM2008 heights",
    "D8 single-flow routing; the stream network depends on the initiation threshold (sensitivity options 0.5-5 km²)",
]


def versions(wbt=None) -> dict:
    import numpy
    import PIL
    import rasterio
    wbt_v = "unknown"
    if wbt is not None:
        try:
            m = re.search(r"v(\d+\.\d+\.\d+)", wbt.version().splitlines()[0])
            wbt_v = m.group(1) if m else wbt.version().splitlines()[0]
        except Exception:  # noqa: BLE001
            pass
    try:
        out = subprocess.run(["gdal_translate", "--version"], capture_output=True, text=True, timeout=30).stdout
        m = re.search(r"GDAL (\d+\.\d+\.\d+)", out)
        gdal_cli = m.group(1) if m else out.strip()
    except Exception:  # noqa: BLE001
        gdal_cli = "unknown"
    try:
        whitebox_py = importlib.metadata.version("whitebox")
    except importlib.metadata.PackageNotFoundError:
        whitebox_py = "unknown"
    return {"pipeline": settings.PIPELINE_VERSION, "whitebox_tools": wbt_v, "whitebox_py": whitebox_py, "gdal_cli": gdal_cli,
            "rasterio": rasterio.__version__, "rasterio_gdal": rasterio.__gdal_version__, "numpy": numpy.__version__,
            "pillow": PIL.__version__, "python": platform.python_version(), "image": settings.LAB_IMAGE_TAG or None}


def config_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


CONDITIONING = {"tool": "breach_depressions", "fill_pits": True, "flat_increment": 0.001}


def compat_config(params: dict, entry: dict, res_m: float) -> dict:
    """The configuration keys that must match for a sub-basin model to be reused (hand/reuse.py COMPAT_KEYS)."""
    return {"pipeline_version": settings.PIPELINE_VERSION, "dem": params["dem"],
            "dem_f32_sha256": (entry.get("files", {}).get("dem_f32.tif") or {}).get("sha256"),
            "district_slug": params["district_slug"], "stream_threshold_cells": params["stream_threshold_cells"],
            "conditioning": CONDITIONING, "flow_routing": "D8", "hand_tool": "elevation_above_stream", "res_m": res_m}


def model_config(job_id: str, params: dict, run: dict, entry: dict, manifest: dict, ver: dict) -> dict:
    """Every parameter that determines the result (hashed → config_hash) plus the ids (not hashed)."""
    hashed = {
        **compat_config(params, entry, run["res"]),
        "terrain_schema": manifest.get("schema"),
        "terrain_manifest_created_at": manifest.get("created_at"),
        "region": params["region"],
        "outlet_snapped": run["snapped_outlet"]["coordinates"],
        "stream_threshold_km2": params["stream_threshold_km2"],
        "window_buffer_m": settings.HAND_WINDOW_BUFFER_M,
        "streams_tool": "extract_streams",
        "slope": {"tool": "slope", "units": "degrees", "source": "original_dem"},
        "links": {"ids": "stream_link_identifier", "catchments": "subbasins", "length": "stream_link_length",
                  "slope": "(z_max - z_min) / length on the breached DEM"},
        "crs": "EPSG:3035",
        "png_grid": {"scheme": "WebMercatorQuad", "zoom": settings.PNG_ZOOM},
        "rating_curves": {"method": "hand_src_manning", "manning_n": settings.SRC_MANNING_N, "slope_min": settings.SRC_SLOPE_MIN,
                          "stage_step_m": settings.SRC_STAGE_STEP_M, "stage_max_m": settings.SRC_STAGE_MAX_M,
                          "short_reach": [settings.SRC_MIN_CATCHMENT_KM2, settings.SRC_MIN_LENGTH_M]},
        "versions": {k: ver.get(k) for k in ("whitebox_tools", "gdal_cli", "rasterio", "rasterio_gdal", "numpy", "pillow", "python")},
    }
    # the reused sub-basin models are not hashed: the result is the same as computing their area (hand/reuse.py)
    reused = [m["hand_model_id"] for m in ((run.get("stats") or {}).get("reuse") or {}).get("models", [])]
    return {**hashed, "config_hash": config_hash(hashed), "hashed_keys": sorted(hashed),
            "hand_model_id": params.get("hand_model_id"), "job_id": job_id, "basin_id": params.get("basin_id"),
            "project_id": params.get("project_id"), "reused_models": reused}


def metadata(job_id: str, params: dict, run: dict, cfg: dict, files: dict, ver: dict, computed_at: str) -> dict:
    dem = settings.DEM_LABELS.get(params["dem"], {"label": params["dem"]})
    return {
        "schema": "geohydroai.hand_model/v1",
        "hand_model_id": params.get("hand_model_id"), "job_id": job_id, "basin_id": params.get("basin_id"),
        "project_id": params.get("project_id"),
        "pipeline_version": settings.PIPELINE_VERSION, "config_hash": cfg["config_hash"],
        "dem": {"source": params["dem"], **dem,
                "staged": {"manifest_schema": cfg["terrain_schema"], "created_at": cfg["terrain_manifest_created_at"],
                           "dem_f32_sha256": cfg["dem_f32_sha256"]}},
        "crs": "EPSG:3035", "res_m": run["res"],
        "region": params["region"], "district_slug": params["district_slug"], "district_name_ua": params.get("district_name_ua"),
        "basin": run["stats"]["basin"], "window": run["window"],
        "stream_threshold_km2": params["stream_threshold_km2"], "stream_threshold_cells": params["stream_threshold_cells"],
        "products": [{"kind": k, "file_name": f["file_name"], "media_type": f["media_type"], "crs": f["crs"],
                      **{x: f["stats"].get(x) for x in ("dtype", "nodata", "units", "encoding")}} for k, f in files.items()],
        "stats": run["stats"], "versions": ver, "timings_s": run["timings_s"], "computed_at": computed_at,
        "method_refs": METHOD_REFS, "limitations": LIMITATIONS,
    }
