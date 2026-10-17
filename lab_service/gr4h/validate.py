"""Request validation of a gr4h_run job (contracts/gr4h_run.v1.json)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lab_service import settings
from lab_service import validate as V

PARAMS_DIR = Path(__file__).parent / "params"


def _iso(s: str, name: str) -> datetime:
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise V.Rejected(f"{name} must be an ISO-8601 UTC time") from None
    if t.tzinfo is None:
        raise V.Rejected(f"{name} must carry a timezone (UTC)")
    return t.astimezone(timezone.utc)


def bundled_params(post_id: str) -> dict:
    p = PARAMS_DIR / f"{post_id}.json"
    if not p.exists():
        raise V.Rejected(f"no GR4H parameter set is published for gauge {post_id}: "
                         f"available {sorted(x.stem for x in PARAMS_DIR.glob('*.json'))}")
    return json.loads(p.read_text())


def prepare_gr4h(params: dict) -> dict:
    """The request as stored with the job: times normalised, the parameter sets resolved (bundled per gauge or inline),
    the forcing checked (hourly, whole days, ≥ 1 year of warm-up) and hashed."""
    f = params.get("forcing")
    if not isinstance(f, dict):
        raise V.Rejected("forcing is required")
    for k in ("time_start", "p_mm", "t_c"):
        if k not in f:
            raise V.Rejected(f"forcing.{k} is required")
    n = len(f["p_mm"])
    if n != len(f["t_c"]) or (f.get("pet_mm") is not None and len(f["pet_mm"]) != n) or (f.get("block_id") is not None and len(f["block_id"]) != n):
        raise V.Rejected("forcing arrays must have the same length")
    if n > settings.GR4H_MAX_HOURS:
        raise V.Rejected(f"forcing longer than {settings.GR4H_MAX_HOURS // 24} days")
    t0 = _iso(f["time_start"], "forcing.time_start")
    if t0.minute or t0.second or t0.hour != 0:
        raise V.Rejected("forcing.time_start must be 00:00 UTC (whole days)")
    if n % 24:
        raise V.Rejected("forcing must cover whole UTC days (a multiple of 24 hours)")
    t_end = t0 + timedelta(hours=n - 1)
    ev = params.get("event") or {}
    run_start = _iso(ev.get("start"), "event.start"); run_end = _iso(ev.get("end"), "event.end")
    if not (t0 <= run_start < run_end <= t_end):
        raise V.Rejected("event.start/end must lie inside the forcing")
    if (run_start - t0) < timedelta(days=settings.GR4H_MIN_WARMUP_DAYS):
        raise V.Rejected(f"the forcing must start at least {settings.GR4H_MIN_WARMUP_DAYS} days before event.start (warm-up)")
    if (run_end - run_start) > timedelta(days=settings.GR4H_MAX_RUN_DAYS):
        raise V.Rejected(f"the event window is longer than {settings.GR4H_MAX_RUN_DAYS} days")
    if any(x is not None and x < 0 for x in f["p_mm"]):
        raise V.Rejected("negative precipitation")
    basin = params.get("basin") or {}
    for k in ("lat", "z_mean_m", "hypso_m", "area_km2"):
        if k not in basin:
            raise V.Rejected(f"basin.{k} is required")
    if len(basin["hypso_m"]) != 101:
        raise V.Rejected("basin.hypso_m must hold 101 elevation quantiles (0 … 100 %)")
    if params.get("parameters") is not None:
        pset = params["parameters"]
        if not isinstance(pset, dict) or not pset.get("sets"):
            raise V.Rejected("parameters.sets must be a non-empty list")
    else:
        if not params.get("post_id"):
            raise V.Rejected("post_id (a gauge with published parameters) or inline parameters are required")
        pset = bundled_params(str(params["post_id"]))
    for s in pset["sets"]:
        for k in ("id", "X1", "X2", "X3", "X4", "CNX1", "CNX2"):
            if k not in s:
                raise V.Rejected(f"parameter set lacks {k}")
    members = params.get("members") or {"mode": "reference"}
    mode = members.get("mode", "reference")
    if mode not in ("reference", "ensemble"):
        raise V.Rejected("members.mode must be reference or ensemble")
    if mode == "ensemble":
        members = {"mode": "ensemble", "alphas": members.get("alphas", [1, 4, 20]), "n_per_alpha": int(members.get("n_per_alpha", 4)),
                   "depth_factors": members.get("depth_factors", [1.0, 1.25]), "seed": int(members.get("seed", 20261017)),
                   "param_sets": members.get("param_sets", "behavioural")}
        n_sets = len(pset["sets"]) if members["param_sets"] == "behavioural" else 1
        total = n_sets * (1 + len(members["alphas"]) * members["n_per_alpha"]) * len(members["depth_factors"])
        if total > settings.GR4H_MAX_MEMBERS:
            raise V.Rejected(f"{total} ensemble members, above the limit {settings.GR4H_MAX_MEMBERS}")
    ht = params.get("hydrotable")
    if ht is not None and not (isinstance(ht, dict) and isinstance(ht.get("reaches"), list) and isinstance(ht.get("stages_m"), list)):
        raise V.Rejected("hydrotable must be a geohydroai.hydrotable/v1 object")
    if params.get("hydrotable_url") is not None and not str(params["hydrotable_url"]).startswith(("https://", "http://")):
        raise V.Rejected("hydrotable_url must be an http(s) URL")
    digest = hashlib.sha256(json.dumps({"time_start": t0.isoformat(), "p_mm": f["p_mm"], "t_c": f["t_c"], "pet_mm": f.get("pet_mm"),
                                        "block_id": f.get("block_id")}, separators=(",", ":")).encode()).hexdigest()
    return {**params, "forcing": {**f, "time_start": t0.isoformat().replace("+00:00", "Z"), "hours": n, "sha256": digest},
            "event": {"start": run_start.isoformat().replace("+00:00", "Z"), "end": run_end.isoformat().replace("+00:00", "Z")},
            "parameters": pset, "members": members}
