"""
National Flash Flood Potential Index (FFPI) at 30 m for one DEM, river-basin district by district (the same 16
districts and cut lines as national_hand.py).

FFPI = (w_s·S + w_l·L + w_h·H + w_v·V) / Σw with every factor 1..10:
  S  slope (deg) from the 30 m DEM, binned (ffpi_config.json factors.s.bins_deg)
  L  land use (ESA WorldCover 2021, ~37 m)        table factors.l
  H  hydrologic soil group A..D of the soil map     table factors.h (gap -> default)
  V  vegetation proxy from the WorldCover classes   table factors.v
Encoding: ffpi.tif Byte = round(FFPI × 10) in 10..100, nodata 0 (water, DEM nodata); s/l/h/v.tif Byte 1..10.
The weights are one published option and are uncalibrated for Ukraine (formula.note in the config).

Per district (EPSG:3035, 30 m, district + 3 km buffer, clipped to the district itself):
  dem.tif (gdalwarp, bilinear, -tap)  ->  slope.tif (WhiteboxTools)
  lc.tif  (gdalwarp -r near of the WorldCover mosaic onto the DEM grid)
  hsg.tif (gdal_rasterize hsg_code of soils_hsg_3035.gpkg onto the DEM grid)
  -> block-wise numpy classification -> {ffpi,s,l,h,v}.tif + run.json (histogram, hsg_gap_px)
Resumable: a district that already has ffpi.tif is skipped.

  docker run --rm -v ./data:/data -v ./scripts:/scripts projects-dem_worker \
    python3 /scripts/national/national_ffpi.py --dem fab_dem [--only 5_1_1]
Inputs:  /data/national/_sources/{esa_worldcover_2021_ua_f4.tif, soils_hsg_3035.gpkg}
Outputs: /data/national/<dem>/ffpi/<slug>/{ffpi,s,l,h,v}.tif, run.json
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

VRT = {"copernicus_dem": "/data/dem/glo30/glo30_4326_aligned.vrt", "fab_dem": "/data/dem/fabdem/fabdem_4326.vrt"}
EPSG, RES = "EPSG:3035", 30.0
CO = ["-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", "-co", "BIGTIFF=YES"]
SOURCES = Path("/data/national/_sources")
LANDCOVER = SOURCES / "esa_worldcover_2021_ua_f4.tif"
SOILS = SOURCES / "soils_hsg_3035.gpkg"
WATER = 80


def sh(*args: str) -> None:
    subprocess.run(list(args), check=True)


def lut(table: dict, size: int = 256) -> np.ndarray:
    out = np.zeros(size, np.uint8)
    for k, v in table.items():
        out[int(k)] = v
    return out


class Tables:
    """Classification tables from ffpi_config.json as numpy lookups."""

    def __init__(self, cfg: dict):
        f = cfg["factors"]
        self.bins = np.asarray(f["s"]["bins_deg"], dtype=np.float32)
        self.l = lut(f["l"]["table"])
        self.v = lut(f["v"]["table"])
        hsg = {"A": 1, "B": 2, "C": 3, "D": 4}
        self.h = np.zeros(5, np.uint8)
        self.h[0] = f["h"]["default"]
        for letter, val in f["h"]["table"].items():
            self.h[hsg[letter]] = val
        w = cfg["formula"]["weights"]
        self.w = np.asarray([w["s"], w["l"], w["h"], w["v"]], dtype=np.float32)
        self.water = set(f["l"].get("nodata_classes", [WATER])) | set(f["v"].get("nodata_classes", []))

    def classify(self, slope: np.ndarray, slope_nodata: float | None, dem_valid: np.ndarray, lc: np.ndarray, hsg: np.ndarray):
        """Returns (ffpi_x10, s, l, h, v) uint8 arrays and the count of land pixels without a soil group."""
        s = (np.digitize(slope, self.bins) + 1).astype(np.uint8)            # 1..10
        l = self.l[lc]
        v = self.v[lc]
        h = self.h[np.clip(hsg, 0, 4)]
        valid = dem_valid & (lc != 0) & (l > 0) & (v > 0)
        if slope_nodata is not None:
            valid &= slope != slope_nodata
        for cls in self.water:
            valid &= lc != cls
        gap = int(np.count_nonzero(valid & (hsg == 0)))
        ffpi = (self.w[0] * s + self.w[1] * l + self.w[2] * h + self.w[3] * v) / self.w.sum()
        out = np.rint(ffpi * 10).astype(np.uint8)
        out[~valid] = 0
        for arr in (s, l, h, v):
            arr[~valid] = 0
        return out, s, l, h, v, gap


def district(a, d: dict, cuts: Path, work: Path, out: Path, tables: Tables) -> dict:
    import rasterio
    from rasterio.windows import Window

    t0 = time.time()
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    dem, slope, lc, hsg = (work / f"{n}.tif" for n in ("dem", "slope", "lc", "hsg"))
    times = {}
    s0 = time.time()
    sh("gdalwarp", "-q", "-overwrite", "-t_srs", EPSG, "-tr", str(RES), str(RES), "-r", "bilinear", "-tap",
       "-cutline", str(cuts / f"{d['slug']}_buf.geojson"), "-crop_to_cutline", "-srcnodata", "-9999", "-dstnodata", "-9999",
       "-ot", "Float32", "-multi", "-wo", "NUM_THREADS=8", "-wm", "2048", *CO, VRT[a.dem], str(dem))
    times["dem"] = round(time.time() - s0, 1)
    with rasterio.open(dem) as r:
        b, w, h_ = r.bounds, r.width, r.height
        profile = r.profile
    te = ["-te", str(b.left), str(b.bottom), str(b.right), str(b.top), "-ts", str(w), str(h_)]
    s0 = time.time()
    import whitebox
    wbt = whitebox.WhiteboxTools()
    wbt.set_verbose_mode(False)
    if wbt.slope(str(dem), str(slope), units="degrees") != 0:
        raise RuntimeError(f"{d['code']}: WhiteboxTools slope failed")
    times["slope"] = round(time.time() - s0, 1)
    s0 = time.time()
    sh("gdalwarp", "-q", "-overwrite", "-t_srs", EPSG, *te, "-r", "near", "-ot", "Byte", "-srcnodata", "0", "-dstnodata", "0",
       "-multi", "-wo", "NUM_THREADS=8", *CO, str(LANDCOVER), str(lc))
    sh("gdal_rasterize", "-q", "-a", "hsg_code", "-l", "soils_hsg", *te, "-ot", "Byte", "-a_nodata", "0", "-init", "0",
       *CO, str(SOILS), str(hsg))
    times["lc_hsg"] = round(time.time() - s0, 1)
    s0 = time.time()
    names = ("ffpi", "s", "l", "h", "v")
    prof = dict(profile, dtype="uint8", nodata=0, count=1, compress="deflate", tiled=True, blockxsize=512, blockysize=512, BIGTIFF="YES")
    hist = np.zeros(101, np.int64)
    gap_px = 0
    land_px = 0
    with rasterio.open(dem) as rd, rasterio.open(slope) as rs, rasterio.open(lc) as rl, rasterio.open(hsg) as rh:
        dsts = {n: rasterio.open(work / f"{n}.tif", "w", **prof) for n in names}
        for row in range(0, h_, 1024):
            win = Window(0, row, w, min(1024, h_ - row))
            dv = rd.read(1, window=win)
            dem_valid = dv != -9999
            arrs = tables.classify(rs.read(1, window=win), rs.nodata, dem_valid, rl.read(1, window=win), rh.read(1, window=win))
            for n, arr in zip(names, arrs[:5]):
                dsts[n].write(arr, 1, window=win)
            gap_px += arrs[5]
            hist += np.bincount(arrs[0].ravel(), minlength=101)[:101]
            land_px += int(np.count_nonzero(arrs[0]))
        for ds in dsts.values():
            ds.close()
    times["classify"] = round(time.time() - s0, 1)
    s0 = time.time()
    for n in names:
        tmp = out / f"{n}.tif.tmp.tif"
        sh("gdalwarp", "-q", "-overwrite", "-cutline", str(cuts / f"{d['slug']}.geojson"), "-dstnodata", "0", *CO, str(work / f"{n}.tif"), str(tmp))
        tmp.replace(out / f"{n}.tif")
    times["clip"] = round(time.time() - s0, 1)
    x = np.arange(101)
    valid_hist = hist[1:]
    mean = float((x[1:] * valid_hist).sum() / max(valid_hist.sum(), 1)) / 10
    run = {"district": d["code"], "name_ua": d["name_ua"], "km2": d["km2"], "dem": a.dem, "config_version": a.cfg["version"],
           "crs": EPSG, "res_m": RES, "buffer_m": 3000, "seconds": round(time.time() - t0, 1), "steps_s": times,
           "land_px_buffered": land_px, "hsg_gap_px": gap_px, "hsg_gap_share": round(gap_px / max(land_px, 1), 4),
           "ffpi_mean_buffered": round(mean, 3), "histogram_x10": hist.tolist(),
           "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (out / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1))
    shutil.rmtree(work, ignore_errors=True)
    return run


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dem", choices=list(VRT), default="fab_dem")
    ap.add_argument("--config", default=str(Path(__file__).with_name("ffpi_config.json")))
    ap.add_argument("--root", default="/data/national")
    ap.add_argument("--only", default="", help="comma list of district slugs (e.g. 5_3_3,M5_2)")
    a = ap.parse_args()
    a.cfg = json.loads(Path(a.config).read_text())
    tables = Tables(a.cfg)
    cuts = Path(a.root) / "_districts"
    only = {c.strip() for c in a.only.split(",") if c.strip()}
    base = Path(a.root) / a.dem / "ffpi"
    for d in json.loads((cuts / "index.json").read_text()):                       # smallest first
        if only and d["slug"] not in only:
            continue
        out = base / d["slug"]
        if (out / "ffpi.tif").exists():
            print(f"skip {d['code']} (done)", flush=True)
            continue
        print(f"{d['code']} {d['name_ua']} {d['km2']:,} km² …", flush=True)
        run = district(a, d, cuts, base / "_work" / d["slug"], out, tables)
        print(f"  done in {run['seconds']:.0f} s {run['steps_s']} mean {run['ffpi_mean_buffered']} gap {run['hsg_gap_share']:.1%}", flush=True)


if __name__ == "__main__":
    main()
