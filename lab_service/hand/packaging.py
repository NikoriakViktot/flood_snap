"""Products of a HAND model: Float32 / Int32 COGs on the EPSG:3035 grid (GDAL 3.4.1 CLI, DEFLATE, nearest overviews)
and the two browser PNGs on a zoom-12 WebMercatorQuad grid, encoded losslessly with Pillow (no colour profile).

hand_3857.png: hand_dm = clip(round(HAND·10), 0, 65534), 65535 = NoData; R = hand_dm >> 8, G = hand_dm & 255,
               B = 1 on a stream cell, A = 255 valid | 0 (R = G = B = 0 then). Alpha is binary on purpose: a 2D canvas
               premultiplies alpha, so only opaque pixels round-trip exactly.
aux_3857.png:  R = clip(round(slope_deg·2), 0, 180), G·256 + B = catchment id (1…65534, 0 = none), A as above."""
from __future__ import annotations

import io
import math
import os
import shutil
import struct
import subprocess
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject, transform_bounds

from lab_service import settings
from lab_service.hand.pipeline import NODATA
from lab_service.pipeline import LabError

ORIGIN = 20037508.342789244          # half the Web Mercator world width (EPSG:3857)
HAND_NODATA_DM = 65535
MAX_CATCHMENT_ID = 65534
COG_PREDICTOR = {"float32": "FLOATING_POINT", "int32": "STANDARD", "uint8": "STANDARD"}


def write_gtiff(path: Path, arr: np.ndarray, transform, crs, nodata, dtype: str) -> Path:
    prof = dict(driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1, dtype=dtype, crs=crs, transform=transform,
                nodata=nodata, tiled=True, blockxsize=256, blockysize=256, compress="deflate")
    with rasterio.open(path, "w", **prof) as d:
        d.write(np.asarray(arr).astype(dtype), 1)
    return path


def cog_translate(src: Path, dst: Path, dtype: str) -> Path:
    """gdal_translate -of COG (GDAL 3.4.1 options verified 2026-10-13): DEFLATE, predictor per dtype, 256-px blocks,
    nearest overviews, one thread (the worker has one CPU)."""
    env = {**os.environ, "GDAL_NUM_THREADS": "1", "GDAL_CACHEMAX": "256"}
    cmd = ["gdal_translate", "-q", "-of", "COG", "-co", "COMPRESS=DEFLATE", "-co", "LEVEL=6", "-co", f"PREDICTOR={COG_PREDICTOR[dtype]}",
           "-co", "BLOCKSIZE=256", "-co", "OVERVIEWS=IGNORE_EXISTING", "-co", "OVERVIEW_RESAMPLING=NEAREST", "-co", "RESAMPLING=NEAREST",
           "-co", "NUM_THREADS=1", "-co", "BIGTIFF=NO", str(src), str(dst)]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"gdal_translate COG failed for {src.name}: {r.stderr.strip()[:500]}")
    return dst


def verify_cog(path: Path) -> dict:
    with rasterio.open(path) as r:
        layout = r.tags(ns="IMAGE_STRUCTURE").get("LAYOUT")
        info = {"layout": layout, "dtype": r.dtypes[0], "nodata": r.nodata, "block": list(r.block_shapes[0]),
                "overviews": r.overviews(1), "shape": [r.height, r.width]}
    if layout != "COG":
        raise RuntimeError(f"{path.name}: GDAL does not report a COG layout ({layout})")
    return info


def mercator_grid(bounds, crs, zoom: int = settings.PNG_ZOOM) -> dict:
    """The zoom-`zoom` WebMercatorQuad cells covering `bounds` (in `crs`): future XYZ tiles align pixel for pixel."""
    left, bottom, right, top = transform_bounds(crs, "EPSG:3857", *bounds)
    n = 256 * 2 ** zoom
    res = 2 * ORIGIN / n
    col0, col1 = math.floor((left + ORIGIN) / res), math.ceil((right + ORIGIN) / res)
    row0, row1 = math.floor((ORIGIN - top) / res), math.ceil((ORIGIN - bottom) / res)
    b3857 = [-ORIGIN + col0 * res, ORIGIN - row1 * res, -ORIGIN + col1 * res, ORIGIN - row0 * res]
    b4326 = transform_bounds("EPSG:3857", "EPSG:4326", *b3857)
    return {"scheme": "WebMercatorQuad", "zoom": zoom, "res_m": res, "col0": col0, "row0": row0, "width": col1 - col0,
            "height": row1 - row0, "transform": from_origin(b3857[0], b3857[3], res, res),
            "bounds_3857": [round(x, 3) for x in b3857], "bounds": [round(x, 9) for x in b4326]}


def to_mercator(arr, transform, crs, grid: dict, nodata, dtype: str) -> np.ndarray:
    out = np.full((grid["height"], grid["width"]), nodata, dtype=dtype)
    reproject(np.asarray(arr, dtype=dtype), out, src_transform=transform, src_crs=crs, src_nodata=nodata,
              dst_transform=grid["transform"], dst_crs="EPSG:3857", dst_nodata=nodata, resampling=Resampling.nearest)
    return out


def encode_hand_png(hand_m: np.ndarray, channel: np.ndarray, valid: np.ndarray) -> np.ndarray:
    dm = np.where(valid, np.clip(np.rint(hand_m * 10), 0, HAND_NODATA_DM - 1), HAND_NODATA_DM).astype(np.uint16)
    rgba = np.zeros(hand_m.shape + (4,), np.uint8)
    rgba[..., 0] = np.where(valid, dm >> 8, 0)
    rgba[..., 1] = np.where(valid, dm & 255, 0)
    rgba[..., 2] = np.where(valid & (channel > 0), 1, 0)
    rgba[..., 3] = np.where(valid, 255, 0)
    return rgba


def decode_hand_png(rgba: np.ndarray):
    """The browser's decoder, for tests: (hand_m, channel, valid)."""
    v = rgba[..., 0].astype(np.uint32) * 256 + rgba[..., 1]
    valid = (rgba[..., 3] == 255) & (v != HAND_NODATA_DM)
    return v.astype(np.float32) / 10, rgba[..., 2] > 0, valid


def encode_aux_png(slope_deg: np.ndarray, catchment: np.ndarray, valid: np.ndarray) -> np.ndarray:
    if int(catchment.max()) > MAX_CATCHMENT_ID:
        raise LabError(f"more than {MAX_CATCHMENT_ID} reach catchments: the browser PNG cannot encode them")
    cid = np.where(valid, catchment, 0).astype(np.uint16)
    rgba = np.zeros(slope_deg.shape + (4,), np.uint8)
    rgba[..., 0] = np.where(valid, np.clip(np.rint(slope_deg * 2), 0, 180), 0)
    rgba[..., 1] = cid >> 8
    rgba[..., 2] = cid & 255
    rgba[..., 3] = np.where(valid, 255, 0)
    return rgba


def decode_aux_png(rgba: np.ndarray):
    valid = rgba[..., 3] == 255
    return rgba[..., 0].astype(np.float32) / 2, rgba[..., 1].astype(np.uint32) * 256 + rgba[..., 2], valid


def png_rgba(rgba: np.ndarray) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgba), "RGBA").save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


def png_chunks(data: bytes) -> list[str]:
    """Chunk types of a PNG (a data PNG must carry no gAMA / sRGB / iCCP that a browser could apply)."""
    out, i = [], 8
    while i + 8 <= len(data):
        n, t = struct.unpack(">I4s", data[i:i + 8])
        out.append(t.decode("ascii"))
        i += 12 + n
    return out


def read_png_rgba(data: bytes) -> np.ndarray:
    from PIL import Image
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGBA"))


def package(run: dict, out_dir: Path) -> dict:
    """Write every raster product of the model into out_dir; returns {kind: {path, file_name, media_type, crs,
    resolution_m, bounds, stats}}. Updates run["stats"]["delivery"] and the quality warnings."""
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "_pack"
    work.mkdir(exist_ok=True)
    crs, tr, res = run["crs"], run["transform"], run["res"]
    files: dict[str, dict] = {}

    def cog(kind: str, name: str, arr, nodata, dtype: str, units=None) -> None:
        src = write_gtiff(work / f"{name}.src.tif", arr, tr, crs, nodata, dtype)
        dst = cog_translate(src, out_dir / f"{name}.tif", dtype)
        src.unlink(missing_ok=True)
        info = verify_cog(dst)
        files[kind] = {"path": dst, "file_name": f"{name}.tif", "media_type": "image/tiff", "crs": crs.to_string(),
                       "resolution_m": res, "bounds": run["bounds_4326"],
                       "stats": {"dtype": dtype, "nodata": nodata, "units": units, "crs": crs.to_string(), "shape": info["shape"],
                                 "cog": True, "overviews": info["overviews"]}}

    cog("hand", "hand", run["hand"], NODATA, "float32", "m")
    cog("slope", "slope", run["slope"], NODATA, "float32", "degrees")
    cog("dem_conditioning", "dem_conditioning", run["cond"], NODATA, "float32", "m")
    cog("stream_links", "stream_links", run["links"], 0, "int32")
    cog("catchments", "catchments", run["catchments"], 0, "int32")

    grid = mercator_grid(run["bounds_3035"], crs)
    npix = grid["width"] * grid["height"]
    if npix > settings.HAND_PNG_MAX_PIXELS:
        run["stats"]["quality"]["warnings"].append(
            f"basin too large for the in-browser PNG ({npix:,} px > {settings.HAND_PNG_MAX_PIXELS:,}): COGs only")
        run["stats"]["delivery"] = "cog_only"
    else:
        hand_m = to_mercator(run["hand"], tr, crs, grid, NODATA, "float32")
        valid = hand_m != NODATA
        chan = to_mercator(run["streams"].astype(np.uint8), tr, crs, grid, 0, "uint8")
        slope_m = to_mercator(run["slope"], tr, crs, grid, NODATA, "float32")
        catch_m = to_mercator(run["catchments"], tr, crs, grid, 0, "int32")
        gstats = {"grid": {k: grid[k] for k in ("scheme", "zoom", "res_m", "col0", "row0", "width", "height")},
                  "bounds_3857": grid["bounds_3857"], "bounds": grid["bounds"], "ground_res_m": res, "crs": "EPSG:3857"}
        for kind, name, rgba, extra in (
                ("hand_png", "hand_3857.png", encode_hand_png(hand_m, chan, valid),
                 {"encoding": "hand_rgba_v1", "nodata": HAND_NODATA_DM, "hand_unit": "dm", "channel": "b", "valid": "a"}),
                ("hand_aux_png", "aux_3857.png", encode_aux_png(slope_m, catch_m, valid & (slope_m != NODATA)),
                 {"encoding": "aux_rgba_v1", "nodata": 255, "slope_unit": "half_degrees", "catchment": "g*256+b", "valid": "a"})):
            dst = out_dir / name
            dst.write_bytes(png_rgba(rgba))
            files[kind] = {"path": dst, "file_name": name, "media_type": "image/png", "crs": "EPSG:3857",
                           "resolution_m": round(grid["res_m"], 3), "bounds": grid["bounds"], "stats": {**gstats, **extra}}
        run["stats"]["delivery"] = "png"
    shutil.rmtree(work, ignore_errors=True)
    return files
