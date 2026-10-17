"""Reuse of finished HAND models of sub-basins (any basin, any nesting depth, any number of sub-basins).

Why it is exact: on the same staged DEM with the same conditioning and stream threshold, a cell's D8 path, its flow
accumulation and therefore its HAND depend only on the cells upstream of it. A sub-basin is closed upstream, so the
HAND of every cell inside it is the same whether the window holds the sub-basin alone or the whole basin (checked on
the staged FABDEM, 2026-10-17: Cheremosh at Usteriky, 1 474 km², against Bilyi Cheremosh at Yablunytsia, 521 km², and
Chornyi Cheremosh at Verkhovyna, 667 km² — HAND identical on all 1.32 M cells, max |Δ| = 0.0 m, streams identical).

How: the platform (geoai-django) sends the candidate models of the same DEM, threshold and district whose outlets lie
inside the new basin (`reuse[]`, contracts/hand_model.v1.json). Here, on the new basin's window of the district D8:
  1. one WhiteboxTools `watershed` with every candidate outlet as a pour point → incremental labels; a candidate whose
     outlet drains into another candidate's area is nested in it, so only the outermost ones are used (their models
     already carry the nested ones); a candidate is used only if its basin has exactly the cell count it was built on;
  2. the outermost sub-basins become NoData in the DEM except their outlet cell (a one-cell "stub" that keeps the
     confluence a junction for the link numbering); the D8 pointer of a stub is the district's;
  3. flow accumulation runs on the pointer, then every sub-basin's cell count is added along the D8 path below its
     stub, so the channels downstream of a confluence carry the whole upstream area; streams, HAND, links and
     catchments are computed for the uncovered part only;
  4. the sub-basins' HAND, conditioning, links, catchments and reaches are pasted back with their link ids shifted
     above the new ones, and the reach that left each sub-basin now flows into the link below its stub. Slope is
     recomputed on the full window (local, cheap, identical). Rating curves are recomputed on the mosaic (same inputs
     per reach, so the same curves) instead of merging JSON.
A candidate built on other terrain, another pipeline or WhiteboxTools version, or one whose files are missing is
skipped with the reason; the job then computes that part itself."""
from __future__ import annotations

import json
import shutil
import urllib.request
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer

from lab_service import settings

FILES = {"hand": "hand.tif", "dem_conditioning": "dem_conditioning.tif", "stream_links": "stream_links.tif",
         "catchments": "catchments.tif", "reaches": "reaches.json", "model_config": "model_config.json"}
# the model_config keys that must be equal for a sub-basin model to be pasted into a new one
COMPAT_KEYS = ("pipeline_version", "dem", "dem_f32_sha256", "district_slug", "stream_threshold_cells", "conditioning",
               "flow_routing", "hand_tool", "res_m")
MAX_CANDIDATES = 64
D8_STEP = {1: (-1, 1), 2: (0, 1), 4: (1, 1), 8: (1, 0), 16: (1, -1), 32: (0, -1), 64: (-1, -1), 128: (-1, 0)}


def _download(url: str, dst: Path) -> None:
    tmp = dst.with_suffix(dst.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:  # noqa: S310  platform-signed URL
        shutil.copyfileobj(r, f)
    tmp.replace(dst)


def fetch(cands: list[dict], tmp: Path) -> tuple[list[dict], list[dict]]:
    """Local copies of each candidate's files: the job folder of this worker when it still exists (JOBS_ROOT/<job_id>,
    kept JOB_TTL_DAYS), else the platform's signed URLs. Returns (loaded, skipped)."""
    loaded, skipped = [], []
    for c in cands[:MAX_CANDIDATES]:
        cid = str(c.get("hand_model_id"))
        local = settings.JOBS_ROOT / str(c.get("job_id") or "-")
        paths: dict[str, Path] = {}
        try:
            for kind, name in FILES.items():
                if (local / name).is_file():
                    paths[kind] = local / name
                    continue
                url = (c.get("files") or {}).get(kind)
                if not url:
                    raise FileNotFoundError(f"{kind} is not available")
                d = tmp / "reuse" / cid
                d.mkdir(parents=True, exist_ok=True)
                _download(url, d / name)
                paths[kind] = d / name
            cfg = json.loads(paths["model_config"].read_text())
            reaches = json.loads(paths["reaches"].read_text())["reaches"]
        except Exception as e:  # noqa: BLE001  a missing or unreadable sub-basin model is computed instead
            skipped.append({"hand_model_id": cid, "reason": f"files: {type(e).__name__}: {e}"[:300]})
            continue
        loaded.append({**c, "hand_model_id": cid, "paths": paths, "config": cfg, "reaches": reaches})
    for c in cands[MAX_CANDIDATES:]:
        skipped.append({"hand_model_id": str(c.get("hand_model_id")), "reason": f"more than {MAX_CANDIDATES} candidates"})
    return loaded, skipped


def compatible(cands: list[dict], current: dict) -> tuple[list[dict], list[dict]]:
    """Keep the candidates built on exactly the current terrain and method (`current` = the new model's config keys)."""
    ok, skipped = [], []
    for c in cands:
        diff = [k for k in COMPAT_KEYS if c["config"].get(k) != current.get(k)]
        if diff:
            skipped.append({"hand_model_id": c["hand_model_id"], "reason": "built differently: " + ", ".join(diff)})
        else:
            ok.append(c)
    return ok, skipped


def select(wbt, cands: list[dict], d8_w: np.ndarray, wtransform, crs, mask_w: np.ndarray, tmp: Path, write) -> dict:
    """The outermost candidate sub-basins inside the new basin (window coordinates). Returns {chosen: [{…, mask, stub}],
    skipped: [...]} — `mask` is the candidate's whole basin on the window, `stub` its outlet cell (row, col)."""
    H, W = mask_w.shape
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    pour = np.full((H, W), -32768, dtype=np.int32)
    placed, skipped = [], []
    for i, c in enumerate(cands, start=1):
        lon, lat = c["outlet"]
        x, y = fwd.transform(lon, lat)
        row, col = rasterio.transform.rowcol(wtransform, x, y)
        if not (0 <= row < H and 0 <= col < W) or not mask_w[row, col]:
            skipped.append({"hand_model_id": c["hand_model_id"], "reason": "its outlet is not inside this basin"})
            continue
        if pour[row, col] != -32768:
            skipped.append({"hand_model_id": c["hand_model_id"], "reason": "same outlet as another candidate"})
            continue
        pour[row, col] = i
        placed.append((i, c, (int(row), int(col))))
    if not placed:
        return {"chosen": [], "skipped": skipped}
    d8_path = write(tmp / "reuse_d8.tif", d8_w, wtransform, crs, 0, "int16")
    pour_path = write(tmp / "reuse_pour.tif", pour, wtransform, crs, -32768, "int32")
    ws_path = tmp / "reuse_ws.tif"
    wbt.set_working_dir(str(tmp))
    if wbt.watershed(str(d8_path), str(pour_path), str(ws_path)) != 0 or not ws_path.exists():
        raise RuntimeError("WhiteboxTools watershed (sub-basins) failed")
    with rasterio.open(ws_path) as r:
        labels = r.read(1, masked=True).filled(0).astype(np.int32)
    labels[labels < 0] = 0
    # parent of each candidate: the label of the cell its outlet drains into (0 = the new basin's own area)
    parent = {}
    for i, _, (row, col) in placed:
        dr, dc = D8_STEP.get(int(d8_w[row, col]), (0, 0))
        r2, c2 = row + dr, col + dc
        parent[i] = int(labels[r2, c2]) if (dr or dc) and 0 <= r2 < H and 0 <= c2 < W else 0
    children: dict[int, list[int]] = {}
    for i, p in parent.items():
        children.setdefault(p, []).append(i)

    def subtree(i: int) -> list[int]:
        out, stack = [], [i]
        while stack:
            k = stack.pop()
            out.append(k)
            stack.extend(children.get(k, []))
        return out

    basin_cells = int(mask_w.sum())
    chosen = []
    for i, c, stub in placed:
        if parent[i] != 0:
            continue                                   # nested in another candidate: that one carries it
        m = np.isin(labels, subtree(i)) & mask_w
        n = int(m.sum())
        want = (c.get("cells") or 0)
        if n >= basin_cells:
            skipped.append({"hand_model_id": c["hand_model_id"], "reason": "it is the same basin"})
            continue
        if want and n != int(want):
            skipped.append({"hand_model_id": c["hand_model_id"],
                            "reason": f"its basin has {n} cells on the current terrain, the model was built on {want}"})
            continue
        chosen.append({**c, "mask": m, "stub": stub, "cells": n})
    for i, c, _ in placed:
        if parent[i] != 0:
            skipped.append({"hand_model_id": c["hand_model_id"], "reason": "nested in another reused sub-basin"})
    return {"chosen": chosen, "skipped": skipped}


def inject(acc: np.ndarray, d8_w: np.ndarray, valid: np.ndarray, chosen: list[dict]) -> int:
    """Add each sub-basin's cell count along the D8 path from its stub to the window edge (in place). Returns the
    number of cells touched."""
    H, W = acc.shape
    touched = 0
    for c in chosen:
        r, col = c["stub"]
        add = float(c["cells"] - 1)                    # the stub counts itself already
        seen = 0
        while 0 <= r < H and 0 <= col < W and valid[r, col] and seen < H * W:
            acc[r, col] += add
            touched += 1
            seen += 1
            dr, dc = D8_STEP.get(int(d8_w[r, col]), (0, 0))
            if not (dr or dc):
                break
            r, col = r + dr, col + dc
    return touched


def read_on(path: Path, transform, shape: tuple[int, int], fill, dtype) -> tuple[np.ndarray, tuple[int, int]]:
    """A sub-basin raster pasted onto the crop grid (`transform`, `shape`); cells it does not cover get `fill`."""
    out = np.full(shape, fill, dtype=dtype)
    with rasterio.open(path) as r:
        a = r.read(1).astype(dtype)
        res = abs(transform.a)
        dc = (r.transform.c - transform.c) / res
        dr = (transform.f - r.transform.f) / res
        if abs(dc - round(dc)) > 1e-3 or abs(dr - round(dr)) > 1e-3 or abs(abs(r.transform.a) - res) > 1e-6:
            raise ValueError(f"{path.name} is not on the district grid")
        dr, dc = int(round(dr)), int(round(dc))
    h, w = a.shape
    r0, c0 = max(dr, 0), max(dc, 0)
    r1, c1 = min(dr + h, shape[0]), min(dc + w, shape[1])
    if r1 > r0 and c1 > c0:
        out[r0:r1, c0:c1] = a[r0 - dr:r1 - dr, c0 - dc:c1 - dc]
    return out, (dr, dc)


def km2(cells: int, res: float) -> float:
    return round(cells * res * res / 1e6, 3)

