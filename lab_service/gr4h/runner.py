"""The child process of a gr4h_run job (spawned by worker.execute with target=gr4h_child, address-space limited).

Writes the forcing, parameter sets and configuration for run.R, runs airGR (Rscript, inherits the memory limit), turns
qsim into the hydrograph product (geohydroai.hydrograph/v1), the stage table on the HAND model's hydrotable when one is
given (geohydroai.stage_table/v1), the model metadata (versions, hashes, the initial state at the end of the warm-up),
and reports them as products of the job. Every output is a model scenario, never an observation."""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import logging
import resource
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from lab_service import settings
from lab_service.pipeline import LabError

log = logging.getLogger("lab")
R_SCRIPT = Path(__file__).parent / "run.R"
STATEMENT = ("Rainfall–runoff model scenario ({model}, airGR) driven by the given forcing: not a measurement, not the "
             "official forecast of the Ukrainian Hydrometeorological Centre, and not a hydraulic model. The first model type "
             "of the GeoHydroAI Flood Lab; other model types will be added.")
LIMITATIONS_COMMON = [
    "lumped model: one precipitation and temperature series for the whole basin; local storms the forcing misses are missed",
    "parameters are a calibrated set for a gauge (or transferred from one), not identifiable from the event itself",
    "stages per reach come from HAND geometry with one Manning n (uniform flow): a screening estimate",
    "runs on a small shared server (2 vCPU, ~8 GB RAM): one job at a time, ensemble size and run length are capped",
]
LIMITATIONS_STEP = {
    "1d": ["daily step: a flood peak inside the day is not resolved; the daily peak underestimates the instantaneous one "
           "(compare with instantaneous maxima, not daily means)",
           "the ensemble is parameter sets × depth factors only: there is no within-day timing at a daily step"],
    "1h": ["hourly step from daily totals: the within-day rain distribution is not observed — evenly spread or sampled "
           "(Dirichlet) in the ensemble; the depth factor is a scenario",
           "parameters were calibrated on hourly forcing built from daily totals: treat hourly peaks as indicative"],
}


def statement(step: str, model: str) -> str:
    return STATEMENT.format(model=model)


UNPUBLISHED = ("this gauge's parameter sets are flagged unreliable: the run serves the network water balance and is not "
               "published as a forecast")


def limitations(step: str, publish: bool = True) -> list[str]:
    return LIMITATIONS_STEP[step] + LIMITATIONS_COMMON + ([] if publish else [UNPUBLISHED])


def model_version(p: dict) -> str | None:
    """The parameter version a run used (params file `version`, e.g. gr4j-era5land-2026.10): carried in every product so
    the platform can expose it as model_version."""
    return p.get("model_version") or p["parameters"].get("version") or ("inline" if not p.get("post_id") else None)


def release_info(p: dict) -> dict:
    """Release, variant and publication flag of the parameter sets (daily releases, params/releases.json). A set that is not
    published (publish false, quality 'unreliable') still runs — the network water balance needs it — but its forecast
    must not be shown as a reliable forecast."""
    ps = p["parameters"]
    return {"model_version": model_version(p), "params_release": p.get("params_release"), "param_variant": ps.get("variant"),
            "publish": bool(ps.get("publish", True)), "quality": ps.get("quality")}


def _step_s(p: dict) -> int:
    return 86400 if p.get("time_step", "1h") == "1d" else 3600
STAGES = {"forcing": 10, "model": 25, "products": 80, "done": 100}


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _write_inputs(p: dict, wd: Path) -> None:
    f = p["forcing"]
    t0 = datetime.fromisoformat(f["time_start"].replace("Z", "+00:00"))
    with (wd / "forcing.csv").open("w", newline="") as fh:
        cols = ["time_utc", "P_mm", "T_C"] + (["PET_mm"] if f.get("pet_mm") is not None else []) + (["block_id"] if f.get("block_id") is not None else [])
        w = csv.writer(fh); w.writerow(cols)
        for i in range(f.get("steps", f["hours"])):
            t = t0.timestamp() + _step_s(p) * i
            row = [datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), f["p_mm"][i] if f["p_mm"][i] is not None else 0.0,
                   f["t_c"][i] if f["t_c"][i] is not None else ""]
            if f.get("pet_mm") is not None:
                row.append(f["pet_mm"][i] if f["pet_mm"][i] is not None else "")
            if f.get("block_id") is not None:
                row.append(f["block_id"][i])
            w.writerow(row)
    (wd / "params.json").write_text(json.dumps({"sets": p["parameters"]["sets"]}))
    b = p["basin"]
    ev = p["event"]
    warm_end = (datetime.fromisoformat(ev["start"].replace("Z", "+00:00")).timestamp() - _step_s(p))
    cfg = {"lat": b["lat"], "z_mean_m": b["z_mean_m"], "hypso_m": b["hypso_m"], "area_km2": b["area_km2"],
           "warmup_start": f["time_start"].replace("Z", "").replace("T", " "),
           "warmup_end": datetime.fromtimestamp(warm_end, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
           "run_start": ev["start"].replace("Z", "").replace("T", " "), "run_end": ev["end"].replace("Z", "").replace("T", " "),
           "members": p["members"], "time_step": p.get("time_step", "1h")}
    if p.get("state_update"):
        cfg["state_update"] = p["state_update"]
    (wd / "config.json").write_text(json.dumps(cfg))


def _read_qsim(wd: Path, area_km2: float, step_s: int = 3600, name: str = "qsim.csv") -> tuple[list[str], list[str], np.ndarray]:
    with (wd / name).open() as fh:
        rows = list(csv.reader(fh))
    head, body = rows[0], rows[1:]
    time_utc = [r[0] for r in body]
    ids = head[1:]
    q_mm = np.array([[float(x) for x in r[1:]] for r in body], dtype=float).T  # members × steps, mm per step
    q_m3s = q_mm * area_km2 * 1e6 / step_s / 1000
    return time_utc, ids, q_m3s


def _members(wd: Path) -> list[dict]:
    with (wd / "members.csv").open() as fh:
        out = []
        for r in csv.DictReader(fh):
            out.append({"id": r["member"], "param_set": r["set"], "hyeto": r["hyeto"],
                        "alpha": None if r["alpha"] in ("", "NA") else float(r["alpha"]), "depth_factor": float(r["depth"])})
    return out


def _series(q: np.ndarray) -> dict:
    from lab_service.gr4h.stages import percentiles
    pct = percentiles(q)
    return {"reference": [round(float(x), 3) for x in q[0]], **{k: [round(float(x), 3) for x in v] for k, v in pct.items()}}


def read_state_update(wd: Path, area_km2: float) -> list[dict] | None:
    """Per member: the routing store before / after the update (mm), ΔR as mm and m³ over the basin, the observed and the
    simulated discharge of the update day (m³/s), and the status (matched, bounded_low/high, rejected)."""
    f = wd / "state_update.csv"
    if not f.exists():
        return None
    k = area_km2 * 1e6 / 86400 / 1000          # mm/day → m³/s
    out = []
    with f.open() as fh:
        for r in csv.DictReader(fh):
            dr = float(r["dR_mm"])
            out.append({"member": r["member"], "status": r["status"], "R_open_loop_mm": float(r["R_open_loop_mm"]),
                        "R_updated_mm": float(r["R_updated_mm"]), "dR_mm": dr, "dR_m3": round(dr / 1000 * area_km2 * 1e6),
                        "q_obs_m3s": round(float(r["q_obs_mm"]) * k, 3), "q_open_loop_m3s": round(float(r["q_open_loop_mm"]) * k, 3),
                        "q_updated_m3s": round(float(r["q_updated_mm"]) * k, 3)})
    return out


def state_update_block(p: dict, q_ol: np.ndarray, q_up: np.ndarray, rows: list[dict]) -> dict:
    """What the update did, with the series of BOTH variants (percentiles + reference), so the shadow comparison and the
    rollback need no second run. `primary` says which variant the hydrograph's main series are."""
    su = p["state_update"]
    st: dict[str, int] = {}
    for r in rows:
        st[r["status"]] = st.get(r["status"], 0) + 1
    applied = [r for r in rows if r["status"] != "rejected"]
    return {"schema": "geohydroai.state_update/v1", "method": "routing store R at the start of the day, bisection so that "
            "the simulated daily Q of the day equals the observed Q (snow and production stores unchanged)",
            "primary": "updated" if su["use"] == "primary" else "open_loop", "use": su["use"], "day": su["day"],
            "q_obs_m3s": su["q_m3s"], "observed_at": su["observed_at"], "issued_at": su.get("issued_at"), "source": su.get("source"),
            "provenance": su.get("provenance"), "max_store_change_frac": su["max_store_change_frac"],
            "status_counts": st, "applied_members": len(applied),
            "dR_mm_median": round(float(np.median([r["dR_mm"] for r in applied])), 3) if applied else None,
            "members": rows, "open_loop": _series(q_ol), "updated": _series(q_up),
            "limitations": ["the bulletin gives the 08:00 instantaneous discharge (from the level through the current rating "
                            "curve); the model day is a daily mean: the update treats one as the other",
                            "only the routing store is adjusted: snow and soil moisture errors are not corrected"]}


def hydrograph_product(p: dict, time_utc: list[str], members: list[dict], q: np.ndarray, su_block: dict | None = None) -> dict:
    from lab_service.gr4h.stages import percentiles
    pct = percentiles(q)
    ref = q[0]
    peak_i = {k: int(np.argmax(v)) for k, v in pct.items()}
    full = q.size <= settings.GR4H_FULL_SERIES_MAX_VALUES
    step = p.get("time_step", "1h")
    step_s = _step_s(p)
    model = p.get("model") or ("CemaNeigeGR4J" if step == "1d" else "CemaNeigeGR4H")
    out = {"schema": "geohydroai.hydrograph/v1", "engine": p.get("engine") or ("airgr-gr4j" if step == "1d" else "airgr-gr4h"),
           "model": model, **release_info(p), "time_step": step, "units": {"q": "m3/s" + (" (daily mean)" if step == "1d" else ""), "p": "mm/d" if step == "1d" else "mm/h"}, "basin_area_km2": p["basin"]["area_km2"], "post_id": p.get("post_id"),
           "forcing_source": p.get("forcing_source"), "forcing_sha256": p["forcing"]["sha256"], "time_utc": time_utc,
           "reference": [round(float(x), 3) for x in ref],
           "p5": [round(float(x), 3) for x in pct["p5"]], "p50": [round(float(x), 3) for x in pct["p50"]], "p95": [round(float(x), 3) for x in pct["p95"]],
           "members_n": len(members),
           "members": [{**m, "peak_m3s": round(float(q[i].max()), 3), "peak_time": time_utc[int(np.argmax(q[i]))]} for i, m in enumerate(members)],
           "peak": {"reference_m3s": round(float(ref.max()), 3), "reference_time": time_utc[int(np.argmax(ref))],
                    **{f"{k}_m3s": round(float(v.max()), 3) for k, v in pct.items()}, **{f"{k}_time": time_utc[peak_i[k]] for k in pct},
                    "volume_reference_mm": round(float(ref.sum() * step_s / (p["basin"]["area_km2"] * 1e6) * 1000), 2)},
           "statement": statement(step, model), "limitations": limitations(step, release_info(p)["publish"])}
    if su_block is not None:
        out["state_update"] = su_block
    if full:
        out["q_by_member"] = [[round(float(x), 2) for x in row] for row in q]
    else:
        out["q_by_member"] = None
        out["note"] = "per-member series omitted (size); peaks per member and the percentiles are kept"
    return out


def _fetch_hydrotable(p: dict) -> dict | None:
    if p.get("hydrotable") is not None:
        return p["hydrotable"]
    url = p.get("hydrotable_url")
    if not url:
        return None
    import httpx
    r = httpx.get(url, timeout=60, follow_redirects=True)
    if r.status_code != 200:
        raise LabError(f"the HAND model's hydrotable could not be fetched ({r.status_code}); re-run when its link is fresh")
    return r.json()


def gr4h_child(job: dict, out_dir: str, conn) -> None:
    try:
        resource.setrlimit(resource.RLIMIT_AS, (settings.CHILD_MEM_BYTES, settings.CHILD_MEM_BYTES))
    except (ValueError, OSError):
        pass
    out = Path(out_dir)
    wd = out / "_tmp"
    wd.mkdir(parents=True, exist_ok=True)
    try:
        p = job["params"]
        conn.send(("progress", STAGES["forcing"], "forcing"))
        _write_inputs(p, wd)
        hydrotable = _fetch_hydrotable(p)
        conn.send(("progress", STAGES["model"], "model"))
        r = subprocess.run(["Rscript", str(R_SCRIPT), str(wd)], capture_output=True, text=True, timeout=settings.GR4H_TIMEOUT_S)
        if r.returncode != 0:
            tail = (r.stderr or r.stdout).strip().splitlines()[-6:]
            raise LabError("airGR run failed: " + " | ".join(tail))
        conn.send(("progress", STAGES["products"], "products"))
        time_utc, ids, q = _read_qsim(wd, p["basin"]["area_km2"], _step_s(p))
        step = p.get("time_step", "1h")
        model = p.get("model") or ("CemaNeigeGR4J" if step == "1d" else "CemaNeigeGR4H")
        engine = p.get("engine") or ("airgr-gr4j" if step == "1d" else "airgr-gr4h")
        members = _members(wd)
        versions = json.loads((wd / "versions.json").read_text())
        state = json.loads((wd / "state.json").read_text())
        su_rows, su_block = None, None
        if p.get("state_update") and (wd / "qsim_updated.csv").exists():
            _, _, q_up = _read_qsim(wd, p["basin"]["area_km2"], _step_s(p), "qsim_updated.csv")
            su_rows = read_state_update(wd, p["basin"]["area_km2"])
            su_block = state_update_block(p, q, q_up, su_rows or [])
            if su_block["primary"] == "updated":
                q = q_up                       # the forecast series, stage table and peaks follow the updated run
        hydro = hydrograph_product(p, time_utc, members, q, su_block)
        products_obj = {"hydrograph": (hydro, "hydrograph.json")}
        stage_tbl = None
        if hydrotable is not None:
            from lab_service.gr4h.stages import stage_table_product
            stage_tbl = stage_table_product(hydrotable, time_utc, q, members)
            products_obj["stage_table"] = (stage_tbl, "stage_table.json")
        computed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        param_sha = _sha(p["parameters"]["sets"])
        meta = {"schema": "geohydroai.gr4h_run/v1", "job_id": job["job_id"], "runoff_run_id": p.get("runoff_run_id"),
                "basin_id": p.get("basin_id"), "hand_model_id": p.get("hand_model_id"), "project_id": p.get("project_id"),
                "pipeline_version": settings.GR4H_PIPELINE_VERSION, **release_info(p), "engine": engine, "model": model, "engine_version": versions.get("airGR"),
                "r_version": versions.get("R"), "image": settings.LAB_IMAGE_TAG, "computed_at": computed_at,
                "time_step": step, "event": p["event"],
                "warmup": {"start": p["forcing"]["time_start"], "steps": int(p["forcing"].get("steps", p["forcing"]["hours"])) - len(time_utc),
                           "hours": int(p["forcing"]["hours"]) - len(time_utc) * _step_s(p) // 3600},
                "forcing": {"source": p.get("forcing_source"), "sha256": p["forcing"]["sha256"], "hours": p["forcing"]["hours"],
                            "steps": p["forcing"].get("steps", p["forcing"]["hours"]), "pet": versions.get("pet")},
                "parameters": {"version": p["parameters"].get("version") or p.get("post_id"), "source": p["parameters"].get("source"),
                               "sets": len(p["parameters"]["sets"]), "set_ids": [x.get("id") for x in p["parameters"]["sets"]],
                               "sha256": param_sha, "transfer": p["parameters"].get("transfer", False),
                               "release": p["parameters"].get("release"), "variant": p["parameters"].get("variant"),
                               "file": p["parameters"].get("file"), "publish": bool(p["parameters"].get("publish", True)),
                               "quality": p["parameters"].get("quality"), "release_note": p["parameters"].get("release_note"),
                               "calibration": p["parameters"].get("calibration"), "skill": p["parameters"].get("skill")},
                "initial_state": {**state, "id": _sha(state)},
                "members": p["members"], "members_n": len(members),
                "state_update": {k: v for k, v in su_block.items() if k not in ("open_loop", "updated", "members")} if su_block else None,
                "statement": statement(step, model), "limitations": limitations(step, release_info(p)["publish"])}
        config_hash = _sha({"forcing": p["forcing"]["sha256"], "parameters": param_sha, "event": p["event"], "members": p["members"], "time_step": step,
                            **({"state_update": p["state_update"]} if p.get("state_update") else {}),
                            "basin": {k: p["basin"][k] for k in ("lat", "z_mean_m", "area_km2")}, "pipeline": settings.GR4H_PIPELINE_VERSION})
        meta["config_hash"] = config_hash
        products_obj["model_metadata"] = (meta, "model_metadata.json")
        for name, (obj, fname) in products_obj.items():
            (out / fname).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        products = []
        for kind, (obj, fname) in products_obj.items():
            data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
            if len(data) > settings.HAND_PRODUCT_MAX_MB * 2 ** 20 and kind == "hydrograph":
                obj = {**obj, "q_by_member": None, "note": "per-member series omitted (size)"}
                data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
            if len(data) > settings.HAND_PRODUCT_MAX_MB * 2 ** 20:
                raise LabError(f"the {kind} product is {len(data) / 2 ** 20:.1f} MB, above the inline limit")
            label = "GR4J" if step == "1d" else "GR4H"
            products.append({"kind": kind, "name": f"{kind.replace('_', ' ')} · {label} · {p.get('post_id') or p.get('basin_id')}",
                             "file_name": fname, "media_type": "application/json", "processing_version": settings.GR4H_PIPELINE_VERSION,
                             "crs": "EPSG:4326", "resolution_m": None, "stats": {"runoff_run": p.get("runoff_run_id"), "bytes": len(data), **release_info(p)},
                             "source_attribution": f"GeoHydroAI Flood Lab {model} {settings.GR4H_PIPELINE_VERSION} — airGR {versions.get('airGR')} (INRAE)",
                             "license": "model output; airGR GPL-2", "content_b64": base64.b64encode(data).decode("ascii")})
        stats = {"members_n": len(members), **release_info(p), "state_update": su_block["primary"] if su_block else None, "time_step": step, "steps": len(time_utc), "hours": len(time_utc) * _step_s(p) // 3600, "peak": hydro["peak"],
                 "stage_table": bool(stage_tbl), "low_confidence_reaches": len(stage_tbl["low_confidence_reaches"]) if stage_tbl else None}
        result = {"kind": "gr4h_run", "stats": stats, "metadata": meta, "config_hash": config_hash, "dropped_products": []}
        (out / "result.json").write_text(json.dumps(result, ensure_ascii=False))
        conn.send(("done", {"result": result, "products": products}))
    except LabError as e:
        conn.send(("failed", str(e)))
    except subprocess.TimeoutExpired:
        conn.send(("failed", f"airGR run exceeded {settings.GR4H_TIMEOUT_S} s"))
    except MemoryError:
        conn.send(("failed", "out of memory: too many ensemble members for this worker"))
    except Exception as e:  # noqa: BLE001
        log.error("job %s crashed:\n%s", job["job_id"], traceback.format_exc())
        conn.send(("failed", f"{type(e).__name__}: {e}"))
    finally:
        conn.close()
