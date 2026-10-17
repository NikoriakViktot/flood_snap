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


MODELS = {"1d": "CemaNeigeGR4J", "1h": "CemaNeigeGR4H"}
ENGINES = {"1d": "airgr-gr4j", "1h": "airgr-gr4h"}


def _params_file(post_id: str, step: str) -> Path:
    # daily sets are <post>.1d.json; the hourly ones keep their first name <post>.json
    return PARAMS_DIR / (f"{post_id}.1d.json" if step == "1d" else f"{post_id}.json")


def published_posts(step: str) -> list[str]:
    files = PARAMS_DIR.glob("*.1d.json") if step == "1d" else (p for p in PARAMS_DIR.glob("*.json") if not p.name.endswith(".1d.json"))
    return sorted(p.name.split(".")[0] for p in files)


def bundled_params(post_id: str, step: str = "1h") -> dict:
    p = _params_file(post_id, step)
    if not p.exists():
        raise V.Rejected(f"no {MODELS[step]} parameter set is published for gauge {post_id}: available {published_posts(step)}")
    d = json.loads(p.read_text())
    if d.get("model") not in (None, MODELS[step]):
        raise V.Rejected(f"the parameter file of gauge {post_id} is for {d.get('model')}, not {MODELS[step]}")
    return d


def prepare_gr4h(params: dict) -> dict:
    """The request as stored with the job: times normalised, the time step and the parameter sets resolved (bundled per
    gauge and step, or inline), the forcing checked (one value per step over whole UTC days, ≥ 1 year of warm-up) and
    hashed. `time_step` 1d (CemaNeige-GR4J, the default) or 1h (CemaNeige-GR4H)."""
    step = params.get("time_step") or settings.DEFAULT_TIME_STEP
    if step not in settings.TIME_STEPS:
        raise V.Rejected(f"time_step must be one of {list(settings.TIME_STEPS)}")
    step_h = 24 if step == "1d" else 1
    f = params.get("forcing")
    if not isinstance(f, dict):
        raise V.Rejected("forcing is required")
    for k in ("time_start", "p_mm", "t_c"):
        if k not in f:
            raise V.Rejected(f"forcing.{k} is required")
    n = len(f["p_mm"])
    if n != len(f["t_c"]) or (f.get("pet_mm") is not None and len(f["pet_mm"]) != n) or (f.get("block_id") is not None and len(f["block_id"]) != n):
        raise V.Rejected("forcing arrays must have the same length")
    max_steps = settings.GR4J_MAX_DAYS if step == "1d" else settings.GR4H_MAX_HOURS
    if n > max_steps:
        raise V.Rejected(f"forcing longer than {max_steps * step_h // 24} days")
    t0 = _iso(f["time_start"], "forcing.time_start")
    if t0.minute or t0.second or t0.hour != 0:
        raise V.Rejected("forcing.time_start must be 00:00 UTC (whole days)")
    if step == "1h" and n % 24:
        raise V.Rejected("hourly forcing must cover whole UTC days (a multiple of 24 hours)")
    t_end = t0 + timedelta(hours=(n - 1) * step_h)
    ev = params.get("event") or {}
    run_start = _iso(ev.get("start"), "event.start"); run_end = _iso(ev.get("end"), "event.end")
    if not (t0 <= run_start < run_end <= t_end):
        raise V.Rejected("event.start/end must lie inside the forcing")
    if (run_start - t0) < timedelta(days=settings.GR4H_MIN_WARMUP_DAYS):
        raise V.Rejected(f"the forcing must start at least {settings.GR4H_MIN_WARMUP_DAYS} days before event.start (warm-up)")
    max_run = settings.GR4J_MAX_RUN_DAYS if step == "1d" else settings.GR4H_MAX_RUN_DAYS
    if (run_end - run_start) > timedelta(days=max_run):
        raise V.Rejected(f"the event window is longer than {max_run} days")
    if step == "1d" and (run_start.hour or run_end.hour or run_start.minute or run_end.minute):
        raise V.Rejected("a daily run's event.start/end must be 00:00 UTC (whole days)")
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
        pset = bundled_params(str(params["post_id"]), step)
    for s in pset["sets"]:
        for k in ("id", "X1", "X2", "X3", "X4", "CNX1", "CNX2"):
            if k not in s:
                raise V.Rejected(f"parameter set lacks {k}")
    members = params.get("members") or {"mode": "reference"}
    mode = members.get("mode", "reference")
    if mode not in ("reference", "ensemble"):
        raise V.Rejected("members.mode must be reference or ensemble")
    if mode == "ensemble":
        # daily: one value per day, so there is no within-day timing to sample — the ensemble is parameter sets × depths
        default_alphas = [] if step == "1d" else [1, 4, 20]
        members = {"mode": "ensemble", "alphas": members.get("alphas", default_alphas) if step == "1h" else [],
                   "n_per_alpha": int(members.get("n_per_alpha", 4)),
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
    return {**params, "time_step": step, "model": MODELS[step], "engine": ENGINES[step],
            "forcing": {**f, "time_start": t0.isoformat().replace("+00:00", "Z"), "steps": n, "hours": n * step_h, "sha256": digest},
            "event": {"start": run_start.isoformat().replace("+00:00", "Z"), "end": run_end.isoformat().replace("+00:00", "Z")},
            "parameters": pset, "members": members}
