"""
One-off: download Copernicus GLO-30 for Ukraine + transboundary upstream catchments.

Source: AWS Open Data mirror s3://copernicus-dem-30m (public, no auth).
Output: data/dem/glo30/tiles/<tile>.tif (original COGs, EPSG:4326, EGM2008 heights)
        data/dem/glo30/tiles.json  (tile list, bytes, sha256, missing = sea/no data)
Resumable: existing tiles with a recorded sha256 are skipped.

Coverage (whole 1° tiles):
  A  Ukraine + margin           21–41°E, 44–53°N
  B  upper Dnipro/Desna/Sozh/   23–37°E, 53–56.5°N  (Dnipro source ≈55.9°N)
     Berezina/Pripyat (BY, RU)
Danube basin is deliberately excluded (gauges there are delta stations; 817k km²).

Usage: python3 scripts/national/fetch_glo30.py [--workers 8] [--out data/dem/glo30]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = "https://copernicus-dem-30m.s3.amazonaws.com"
REGIONS = {
    "ukraine": (21.0, 44.0, 41.0, 53.0),
    "upper_dnipro": (23.0, 53.0, 37.0, 56.5),
}


def tile_name(lat: int, lon: int) -> str:
    ns, ew = ("N" if lat >= 0 else "S"), ("E" if lon >= 0 else "W")
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def tiles_for(boxes) -> list[str]:
    names = set()
    for w, s, e, n in boxes:
        for lat in range(math.floor(s), math.ceil(n)):
            for lon in range(math.floor(w), math.ceil(e)):
                names.add(tile_name(lat, lon))
    return sorted(names)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(name: str, out: Path, retries: int = 4) -> dict:
    dst = out / f"{name}.tif"
    url = f"{BASE}/{name}/{name}.tif"
    for attempt in range(retries):
        try:
            tmp = dst.with_suffix(".part")
            with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            tmp.replace(dst)
            return {"tile": name, "status": "ok", "bytes": dst.stat().st_size, "sha256": sha256(dst)}
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):  # sea / outside the product
                return {"tile": name, "status": "missing"}
            err = f"HTTP {e.code}"
        except Exception as e:  # network hiccup → retry
            err = f"{type(e).__name__}: {e}"
        time.sleep(2 * (attempt + 1))
    return {"tile": name, "status": "error", "error": err}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/dem/glo30")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    root = Path(a.out)
    tiles_dir = root / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    index_path = root / "tiles.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}

    wanted = tiles_for(REGIONS.values())
    todo = [t for t in wanted
            if index.get(t, {}).get("status") not in ("ok", "missing")
            or (index[t]["status"] == "ok" and not (tiles_dir / f"{t}.tif").exists())]
    print(f"{len(wanted)} tiles in coverage, {len(todo)} to fetch", flush=True)

    done = 0
    with ThreadPoolExecutor(a.workers) as pool:
        futs = {pool.submit(fetch, t, tiles_dir): t for t in todo}
        for f in as_completed(futs):
            r = f.result()
            index[r["tile"]] = r
            done += 1
            if done % 10 == 0 or r["status"] == "error":
                index_path.write_text(json.dumps(index, indent=1))
                print(f"[{done}/{len(todo)}] {r['tile']} {r['status']}", flush=True)
    index_path.write_text(json.dumps(index, indent=1))

    ok = [v for v in index.values() if v["status"] == "ok"]
    errors = [v for v in index.values() if v["status"] == "error"]
    gb = sum(v["bytes"] for v in ok) / 2**30
    print(f"DONE ok={len(ok)} missing={sum(v['status'] == 'missing' for v in index.values())} "
          f"errors={len(errors)} size={gb:.2f} GiB", flush=True)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
