"""
First HAND-model experiment on production (plan M1 §11.6): the small Prut mountain sub-basin, both DEMs, four
stream-initiation thresholds, through the platform API (never through the tile servers).

  python3 scripts/experiments/hand_prut_m1.py --base https://geohydroai.org --project <uuid> \
      [--outlet 24.7598 48.1487 --region 5.3.2] [--dems fab_dem,copernicus_dem] [--thresholds 0.5,1,2,5] \
      [--out data/experiments/hand_prut_m1]

Signs in with the e-mail given in --email (password asked interactively or piped with --password-stdin, never stored), builds or reuses the basin
delineation at the outlet, submits the HAND models (the API reuses identical finished models), polls the jobs, saves
every product (COGs, PNGs, JSON) under --out/<dem>_<threshold>/ and prints a comparison table: timings, HAND
mean/p90, share ≤ 1/2/3 m, stream length / links / density, slope mean, conditioning carve, exploratory area at 2 m.
Needs only the standard library.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class Api:
    def __init__(self, base: str, token: str | None = None):
        self.base, self.token = base.rstrip("/") + "/api/v1", token

    def call(self, method: str, path: str, body: dict | None = None, auth: bool = True):
        url = path if path.startswith("http") else self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if auth and self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw and r.headers.get_content_type() == "application/json" else raw)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw

    def login(self, email: str, password: str) -> None:
        status, body = self.call("POST", "/auth/login", {"email": email, "password": password}, auth=False)
        if status != 200:
            sys.exit(f"login failed: {status} {body}")
        self.token = body["access"]


def wait_job(api: Api, job_id: str, label: str, timeout_s: int = 900) -> dict:
    t0, delay = time.time(), 1.0
    while time.time() - t0 < timeout_s:
        status, job = api.call("GET", f"/jobs/{job_id}")
        if status != 200:
            sys.exit(f"{label}: GET /jobs/{job_id} → {status} {job}")
        if job["status"] in ("done", "failed", "cancelled"):
            print(f"  {label}: {job['status']} after {time.time() - t0:.0f} s" + (f" — {job['error']}" if job.get("error") else ""))
            return job
        print(f"  {label}: {job['status']} {job['progress_pct']} %", end="\r", flush=True)
        time.sleep(delay)
        delay = min(delay * 1.5, 10)
    sys.exit(f"{label}: timeout after {timeout_s} s")


def ensure_basin(api: Api, project: str, lon: float, lat: float, region: str) -> dict:
    status, basins = api.call("GET", f"/projects/{project}/basin-delineations")
    if status == 200:
        for b in basins:
            o = (b.get("outlet") or {}).get("coordinates") or []
            if b.get("basin") and b.get("region") == region and len(o) == 2 and abs(o[0] - lon) < 1e-4 and abs(o[1] - lat) < 1e-4:
                print(f"basin reused: {b['id']} ({b['stats'].get('area_km2')} km²)")
                return b
    status, r = api.call("POST", f"/projects/{project}/basin-delineation",
                         {"outlet_lon": lon, "outlet_lat": lat, "hand_threshold": 2, "stream_threshold": "medium", "region": region})
    if status != 202:
        sys.exit(f"basin delineation refused: {status} {r}")
    job = wait_job(api, r["job_id"], "basin")
    if job["status"] != "done":
        sys.exit("basin delineation failed")
    status, b = api.call("GET", f"/basin-delineations/{r['basin_id']}")
    print(f"basin built: {b['id']} ({b['stats'].get('area_km2')} km², IoU {((b['stats'].get('comparison') or {}).get('iou'))})")
    return b


def run_model(api: Api, project: str, basin_id: str, dem: str, thr: float, out: Path) -> dict | None:
    label = f"{dem} {thr:g} km²"
    status, r = api.call("POST", f"/projects/{project}/hand-models", {"basin_id": basin_id, "dem": dem, "stream_threshold_km2": thr})
    if status not in (200, 202):
        print(f"  {label}: refused {status} {r}")
        return None
    if r.get("reused"):
        print(f"  {label}: reused model {r['hand_model_id']}")
    else:
        job = wait_job(api, r["job_id"], label)
        if job["status"] != "done":
            return None
    status, m = api.call("GET", f"/hand-models/{r['hand_model_id']}")
    if status != 200:
        print(f"  {label}: GET model → {status}")
        return None
    d = out / f"{dem}_{thr:g}km2"
    d.mkdir(parents=True, exist_ok=True)
    (d / "hand_model.json").write_text(json.dumps(m, ensure_ascii=False, indent=1))
    for p in m.get("products") or []:
        name = (p.get("stats") or {}).get("file_name") or f"{p['kind']}.bin"
        status, data = api.call("GET", p["url"], auth=False)
        if status == 200 and isinstance(data, (bytes, bytearray)):
            (d / name).write_bytes(data)
        elif status == 200:
            (d / name).write_text(json.dumps(data, ensure_ascii=False, indent=1))
        else:
            print(f"  {label}: {name} download → {status}")
    return m


def row(m: dict) -> list[str]:
    s, t = m.get("stats") or {}, (m.get("metadata") or {}).get("timings_s") or {}
    hand, st, sl, c = s.get("hand", {}), s.get("streams", {}), s.get("slope", {}), s.get("conditioning", {})
    at2 = next((x["area_km2"] for x in s.get("flood_levels", []) if x["level_m"] == 2), None)
    f = lambda v, d=2: "—" if v is None else f"{v:.{d}f}"
    return [m["dem"], f"{float(m['stream_threshold_km2']):g}", f(sum(t.values()), 1), f(hand.get("mean")), f(hand.get("p90")),
            f((hand.get("share_le") or {}).get("1"), 3), f((hand.get("share_le") or {}).get("3"), 3), f(st.get("length_km"), 1),
            str(st.get("links", "—")), f(st.get("drainage_density_km_km2"), 2), f(sl.get("mean"), 1), str(c.get("cells_changed", "—")),
            f(c.get("max_carve_m")), f(at2), s.get("delivery", "—")]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://geohydroai.org")
    ap.add_argument("--project", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--outlet", nargs=2, type=float, default=[24.7598, 48.1487], metavar=("LON", "LAT"))
    ap.add_argument("--region", default="5.3.2")
    ap.add_argument("--dems", default="fab_dem,copernicus_dem")
    ap.add_argument("--thresholds", default="0.5,1,2,5")
    ap.add_argument("--out", default="data/experiments/hand_prut_m1")
    ap.add_argument("--password-stdin", action="store_true", help="read the password from stdin (piped from a secrets file)")
    a = ap.parse_args()
    api = Api(a.base)
    api.login(a.email, sys.stdin.readline().strip() if a.password_stdin else getpass.getpass("platform password: "))
    basin = ensure_basin(api, a.project, a.outlet[0], a.outlet[1], a.region)
    out = Path(a.out)
    models = []
    for dem in a.dems.split(","):
        for thr in (float(x) for x in a.thresholds.split(",")):
            m = run_model(api, a.project, basin["id"], dem.strip(), thr, out)
            if m:
                models.append(m)
    head = ["dem", "km²", "t s", "HAND mean", "p90", "≤1 m", "≤3 m", "streams km", "links", "dens", "slope°", "cond cells", "carve m", "A(2 m) km²", "delivery"]
    rows = [head] + [row(m) for m in models]
    widths = [max(len(r[i]) for r in rows) for i in range(len(head))]
    for r in rows:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))
    (out / "summary.json").write_text(json.dumps([{"id": m["id"], "dem": m["dem"], "stream_threshold_km2": m["stream_threshold_km2"],
                                                   "config_hash": m.get("config_hash"), "stats": m.get("stats"),
                                                   "timings_s": (m.get("metadata") or {}).get("timings_s")} for m in models],
                                                 ensure_ascii=False, indent=1))
    print(f"\nproducts and summary.json under {out}")


if __name__ == "__main__":
    main()
