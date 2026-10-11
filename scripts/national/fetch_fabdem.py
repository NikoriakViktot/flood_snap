"""
One-off: download FABDEM v1-2 for the same coverage as fetch_glo30.py.

FABDEM (Hawker et al. 2022, University of Bristol) = Copernicus GLO-30 with forest and building
height biases removed. Licence CC BY-NC-SA 4.0 (non-commercial). Heights EGM2008, EPSG:4326.

Uses the `fabdem` package (byte-range extraction from the 10°x10° ZIPs on data.bris.ac.uk) one 1°
tile at a time: fabdem.download() merges in RAM, so asking for the whole country at once would need
>12 GB. Each call caches the tile; we keep only the cached tile files.

Output: data/dem/fabdem/tiles/N47E024_FABDEM_V1-2.tif …, data/dem/fabdem/tiles.json
Needs a Python with `fabdem` (e.g. ~/repo/SWOT-DNIPRO/.venv/bin/python). Resumable.

Usage: <python-with-fabdem> scripts/national/fetch_fabdem.py [--seed-cache DIR] [--workers 4]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fetch_glo30 import REGIONS  # same coverage as GLO-30  # noqa: E402

import math  # noqa: E402
import socket  # noqa: E402

# the fabdem package sets no network timeouts; a stalled read hung the whole run for an hour
socket.setdefaulttimeout(180)


def tile_id(lat: int, lon: int) -> str:
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}_FABDEM_V1-2"


def wanted() -> list[tuple[int, int]]:
    out = set()
    for w, s, e, n in REGIONS.values():
        for lat in range(math.floor(s), math.ceil(n)):
            for lon in range(math.floor(w), math.ceil(e)):
                out.add((lat, lon))
    return sorted(out)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(lat: int, lon: int, tiles: Path, cache: Path) -> dict:
    import fabdem

    tid = tile_id(lat, lon)
    dst = tiles / f"{tid}.tif"
    if dst.exists():
        return {"tile": tid, "status": "ok", "bytes": dst.stat().st_size, "sha256": sha256(dst)}
    # bounds strictly inside the tile so only that tile is requested
    b = (lon + 0.25, lat + 0.25, lon + 0.75, lat + 0.75)
    with tempfile.TemporaryDirectory() as td:
        try:
            fabdem.download(b, output_path=str(Path(td) / "x.tif"), show_progress=False, cache=cache)
        except Exception as e:  # no land tile (sea) or network
            msg = f"{type(e).__name__}: {e}"
            # fabdem raises IndexError when the 1° tile has no land (sea) in its ZIP listing
            sea = isinstance(e, IndexError) or ("no" in msg.lower() and "tile" in msg.lower())
            return {"tile": tid, "status": "missing" if sea else "error",
                    "error": msg[:300]}
    src = cache / f"{tid}.tif"
    if not src.exists():
        return {"tile": tid, "status": "missing"}
    shutil.move(str(src), dst)
    return {"tile": tid, "status": "ok", "bytes": dst.stat().st_size, "sha256": sha256(dst)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/dem/fabdem")
    ap.add_argument("--seed-cache", default=None, help="dir with already downloaded *_FABDEM_V1-2.tif")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    root = Path(a.out)
    tiles = root / "tiles"
    cache = root / "_cache"
    tiles.mkdir(parents=True, exist_ok=True)
    cache.mkdir(exist_ok=True)
    index_path = root / "tiles.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}

    want = wanted()
    if a.seed_cache:
        for lat, lon in want:
            s = Path(a.seed_cache) / f"{tile_id(lat, lon)}.tif"
            if s.exists() and not (tiles / s.name).exists():
                shutil.copy2(s, tiles / s.name)
                print(f"seeded {s.name}", flush=True)

    todo = [(lat, lon) for lat, lon in want if index.get(tile_id(lat, lon), {}).get("status") != "missing"
            and not (index.get(tile_id(lat, lon), {}).get("status") == "ok" and (tiles / f"{tile_id(lat, lon)}.tif").exists())]
    print(f"{len(want)} tiles in coverage, {len(todo)} to fetch", flush=True)

    done = 0
    with ThreadPoolExecutor(a.workers) as pool:
        futs = [pool.submit(fetch, lat, lon, tiles, cache) for lat, lon in todo]
        for f in as_completed(futs):
            r = f.result()
            index[r["tile"]] = r
            done += 1
            if done % 10 == 0 or r["status"] == "error":
                index_path.write_text(json.dumps(index, indent=1))
                print(f"[{done}/{len(todo)}] {r['tile']} {r['status']} {r.get('error', '')}", flush=True)
    index_path.write_text(json.dumps(index, indent=1))
    ok = [v for v in index.values() if v["status"] == "ok"]
    err = [v for v in index.values() if v["status"] == "error"]
    print(f"DONE ok={len(ok)} missing={sum(v['status'] == 'missing' for v in index.values())} errors={len(err)} "
          f"size={sum(v['bytes'] for v in ok) / 2**30:.2f} GiB", flush=True)
    shutil.rmtree(cache, ignore_errors=True)
    sys.exit(1 if err else 0)


if __name__ == "__main__":
    main()
