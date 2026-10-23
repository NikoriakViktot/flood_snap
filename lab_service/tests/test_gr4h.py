"""GR4J (daily, default) / GR4H (hourly) runs: the discharge → stage rule (identical to the browser's), request validation, and the job child's events
against the v1.2 contract with a fake Rscript (the real airGR run is covered by test_gr4h_r when Rscript is present)."""
from __future__ import annotations

import csv
import json
import multiprocessing as mp
import shutil
from pathlib import Path

import jsonschema
import numpy as np
import pytest

C = Path(__file__).resolve().parents[1] / "contracts"
EVENT = json.loads((C / "job_event.v1.json").read_text())
INPUT = json.loads((C / "gr4h_run.v1.json").read_text())

HT = {"schema": "geohydroai.hydrotable/v1", "stages_m": [0.0, 1.0, 2.0, 3.0],
      "reaches": [{"id": 1, "to": 0, "drainage_area_km2": 100.0, "discharge_m3s": [0.0, 10.0, 40.0, 90.0]},
                  {"id": 2, "to": 1, "drainage_area_km2": 25.0, "discharge_m3s": [0.0, 5.0, 20.0, 45.0]},
                  {"id": 3, "to": 1, "drainage_area_km2": None, "discharge_m3s": []}]}


def _forcing(days=400, start="2019-01-01T00:00:00Z"):
    n = 24 * days
    return {"time_start": start, "p_mm": [0.1] * n, "t_c": [5.0] * n, "pet_mm": None, "block_id": None}


def _daily(days=400, start="2019-01-01T00:00:00Z"):
    return {"time_start": start, "p_mm": [2.0] * days, "t_c": [5.0] * days, "pet_mm": None, "block_id": None}


def _params(**over):
    base = {"runoff_run_id": "r1", "basin_id": "b1", "hand_model_id": None, "project_id": "p1", "post_id": "42194", "time_step": "1h",
            "basin": {"lat": 47.9, "z_mean_m": 1150.0, "hypso_m": [600 + 10 * i for i in range(101)], "area_km2": 552.0},
            "forcing": _forcing(), "event": {"start": "2020-01-20T00:00:00Z", "end": "2020-01-28T23:00:00Z"},
            "members": {"mode": "reference"}}
    base.update(over)
    return base


# ---- stages (hand/src.py stage_for + src/domain/hydrotable.ts qTransfer / stageTable) -------------------------------
def test_stage_rule_matches_the_browser():
    from lab_service.gr4h import stages as S
    r1 = HT["reaches"][0]
    assert S.stage_for(r1, HT["stages_m"], 25.0) == pytest.approx(1.5)       # linear between 10 → 1 m and 40 → 2 m
    assert S.stage_for(r1, HT["stages_m"], -1.0) == 0.0 and S.stage_for(r1, HT["stages_m"], 1000.0) == 3.0  # clamped
    assert S.q_transfer(100.0, 100.0, 25.0) == pytest.approx(100.0 * 0.25 ** 0.8)
    assert S.q_transfer(0.0, 100.0, 25.0) == 0.0
    assert S.outlet_area(HT) == 100.0
    t = S.stage_table(HT, 40.0, 100.0)
    assert t[1] == pytest.approx(2.0) and 3 not in t
    q2 = 40.0 * 0.25 ** 0.8                                                    # 13.19 → between 5 → 1 m and 20 → 2 m
    assert t[2] == pytest.approx(1.0 + (q2 - 5.0) / 15.0)
    assert S.transfer_confidence(100.0, 25.0) == "low" and S.transfer_confidence(100.0, 60.0) == "high"


def test_stage_table_product_percentiles_and_exceedance():
    from lab_service.gr4h.stages import stage_table_product
    time_utc = [f"2020-01-20T{h:02d}:00:00Z" for h in range(4)]
    q = np.array([[1.0, 10.0, 40.0, 5.0], [1.0, 20.0, 90.0, 5.0], [1.0, 30.0, 140.0, 5.0]])  # 3 members × 4 steps
    members = [{"id": f"m{i}"} for i in range(3)]
    p = stage_table_product(HT, time_utc, q, members)
    jsonschema.Draft202012Validator.check_schema({"type": "object"})
    assert p["schema"] == "geohydroai.stage_table/v1" and p["reach_ids"] == [1, 2] and p["peak_step"] == 2
    assert p["steps"][2]["q_m3s"]["p50"] == 90.0 and p["steps"][2]["stages_m"]["p50"][0] == pytest.approx(3.0)
    assert len(p["stage_p50_by_step"]) == 4 and p["stage_p50_by_step"][0] == [pytest.approx(0.1, abs=0.01), pytest.approx(0.07, abs=0.01)]  # 1 m³/s → reach 2: 1·0.25^0.8 = 0.33 → 0.066 m
    ex = p["exceedance_at_peak"]
    # peak step: reach 1 stages 2.0 / 3.0 / 3.0 (clamped) for Q 40 / 90 / 140 → strictly above 2 m: two of three, above 3 m: none
    assert ex["thresholds_m"] == [0.5, 1, 2, 3] and ex["share_by_reach"][0] == [1.0, 1.0, pytest.approx(0.667, abs=1e-3), 0.0]
    assert p["low_confidence_reaches"] == [2]


# ---- validation ---------------------------------------------------------------------------------------------------------
def test_input_contract_and_prepare():
    from lab_service.gr4h.validate import prepare_gr4h
    params = _params()
    jsonschema.validate({"job_id": "j", "params": params}, INPUT)
    p = prepare_gr4h(params)
    assert p["forcing"]["hours"] == 9600 and len(p["forcing"]["sha256"]) == 64
    assert p["parameters"]["sets"][0]["id"] == "reference_2004_2010_F3" and len(p["parameters"]["sets"]) == 31
    assert p["members"] == {"mode": "reference"}


@pytest.mark.parametrize("over, msg", [
    ({"forcing": _forcing(days=100), "event": {"start": "2019-03-01T00:00:00Z", "end": "2019-03-05T23:00:00Z"}}, "warm-up"),
    ({"forcing": _forcing(start="2019-01-01T06:00:00Z")}, "00:00 UTC"),
    ({"forcing": _forcing(days=460), "event": {"start": "2020-01-20T00:00:00Z", "end": "2020-03-01T00:00:00Z"}}, "longer than"),
    ({"post_id": "00000"}, "no CemaNeigeGR4H parameter set"),
    ({"time_step": "6h"}, "time_step must be"),
    ({"basin": {"lat": 47.9, "z_mean_m": 1150.0, "hypso_m": [1, 2, 3], "area_km2": 552.0}}, "101"),
    ({"members": {"mode": "ensemble", "alphas": [1, 4, 20], "n_per_alpha": 50, "depth_factors": [1, 1.25]}}, "above the limit"),
    ({"hydrotable": {"schema": "x"}}, "hydrotable"),
])
def test_prepare_rejects(over, msg):
    from lab_service import validate as V
    from lab_service.gr4h.validate import prepare_gr4h
    with pytest.raises(V.Rejected, match=msg):
        prepare_gr4h(_params(**over))


def test_daily_is_the_default_with_gr4j_sets():
    from lab_service.gr4h.validate import prepare_gr4h, published_posts
    params = _params(forcing=_daily(), event={"start": "2020-01-20T00:00:00Z", "end": "2020-02-03T00:00:00Z"})
    params.pop("time_step")
    jsonschema.validate({"job_id": "j", "params": params}, INPUT)
    p = prepare_gr4h(params)
    assert p["time_step"] == "1d" and p["model"] == "CemaNeigeGR4J" and p["engine"] == "airgr-gr4j"
    assert p["forcing"]["steps"] == 400 and p["forcing"]["hours"] == 9600
    assert p["parameters"]["model"] == "CemaNeigeGR4J" and p["parameters"]["sets"][0]["id"] == "reference"
    assert len(p["parameters"]["sets"]) == 4 and "42136" in published_posts("1d") and "42194" in published_posts("1h")
    # a daily ensemble has no within-day timing: parameter sets × depth factors only
    e = prepare_gr4h({**params, "members": {"mode": "ensemble", "alphas": [1, 4], "depth_factors": [1.0, 1.25]}})
    assert e["members"]["alphas"] == []


@pytest.mark.parametrize("over, msg", [
    ({"forcing": _daily(days=200), "event": {"start": "2019-06-01T00:00:00Z", "end": "2019-06-10T00:00:00Z"}}, "warm-up"),
    ({"forcing": _daily(days=800), "event": {"start": "2020-01-20T00:00:00Z", "end": "2021-02-01T00:00:00Z"}}, "longer than 366"),
    ({"event": {"start": "2020-01-20T06:00:00Z", "end": "2020-01-25T00:00:00Z"}}, "whole days"),
    ({"post_id": "42253"}, "no CemaNeigeGR4J parameter set"),
])
def test_daily_rejects(over, msg):
    from lab_service import validate as V
    from lab_service.gr4h.validate import prepare_gr4h
    base = _params(time_step="1d", forcing=_daily(), event={"start": "2020-01-20T00:00:00Z", "end": "2020-01-31T00:00:00Z"})
    with pytest.raises(V.Rejected, match=msg):
        prepare_gr4h({**base, **over})


def test_ensemble_member_count():
    from lab_service.gr4h.validate import prepare_gr4h
    p = prepare_gr4h(_params(members={"mode": "ensemble", "alphas": [1, 4], "n_per_alpha": 2, "depth_factors": [1.0, 1.25]}))
    assert p["members"]["param_sets"] == "behavioural" and p["members"]["seed"] == 20261017


# ---- the job child with a fake Rscript ----------------------------------------------------------------------------------
def _fake_rscript(monkeypatch):
    """subprocess.run stand-in: writes what run.R would (qsim.csv, members.csv, state.json, versions.json)."""
    import subprocess

    def run(cmd, **kw):
        wd = Path(cmd[-1])
        cfg = json.loads((wd / "config.json").read_text())
        with (wd / "forcing.csv").open() as fh:
            times = [r["time_utc"] for r in csv.DictReader(fh)]
        t0, t1 = cfg["run_start"].replace(" ", "T") + "Z", cfg["run_end"].replace(" ", "T") + "Z"
        run_t = [t for t in times if t0 <= t <= t1]
        members = ["m0000", "m0001"] if cfg["members"]["mode"] == "ensemble" else ["m0000"]
        with (wd / "qsim.csv").open("w", newline="") as fh:
            w = csv.writer(fh); w.writerow(["time_utc", *members])
            for i, t in enumerate(run_t):
                w.writerow([t, *[round(0.01 + 0.002 * i * (k + 1), 5) for k in range(len(members))]])
        with (wd / "members.csv").open("w", newline="") as fh:
            w = csv.writer(fh); w.writerow(["member", "set", "hyeto", "alpha", "depth"])
            for k, m in enumerate(members):
                w.writerow([m, "reference_2004_2010_F3", "even" if k == 0 else "a1_01", "NA" if k == 0 else 1, 1.0])
        (wd / "state.json").write_text(json.dumps({"schema": "geohydroai.gr4h_state/v1", "states": {"Store": [0.5, 0.3]}}))
        (wd / "versions.json").write_text(json.dumps({"R": "R version 4.1.2", "airGR": "1.7.9", "members": len(members), "pet": "Oudin"}))
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", run)


def _run_child(tmp_path, params):
    """The job child in a thread, its events read meanwhile: a result larger than the pipe buffer (~64 KB) would block
    a child that runs before anyone reads (in operation the child is a process and the worker reads as it goes)."""
    import threading

    from lab_service.gr4h.runner import gr4h_child
    parent, child = mp.Pipe(duplex=False)
    t = threading.Thread(target=gr4h_child, args=({"job_id": "j1", "params": params}, str(tmp_path / "out"), child), daemon=True)
    t.start()
    events = []
    while True:
        try:
            events.append(parent.recv())
        except EOFError:
            break
    t.join(timeout=600)
    return events


def test_child_done_event_matches_contract(tmp_path, monkeypatch):
    from lab_service.gr4h.validate import prepare_gr4h
    _fake_rscript(monkeypatch)
    p = prepare_gr4h(_params(hydrotable=HT, members={"mode": "ensemble", "alphas": [1], "n_per_alpha": 1, "depth_factors": [1.0], "param_sets": "reference"}))
    events = _run_child(tmp_path, p)
    assert [e[0] for e in events] == ["progress", "progress", "progress", "done"]
    payload = events[-1][1]
    body = {"status": "done", "progress_pct": 100, "stage": "done", "error": None, "result_ref": "lab:j1",
            "result": payload["result"], "products": payload["products"]}
    jsonschema.validate({"job_id": "j1", "payload": body}, EVENT)
    kinds = [x["kind"] for x in payload["products"]]
    assert kinds == ["hydrograph", "stage_table", "model_metadata"]
    r = payload["result"]
    assert r["kind"] == "gr4h_run" and r["stats"]["members_n"] == 2 and r["stats"]["hours"] == 9 * 24
    assert r["metadata"]["initial_state"]["id"] and r["metadata"]["parameters"]["sets"] == 31
    hydro = json.loads(__import__("base64").b64decode(payload["products"][0]["content_b64"]))
    assert hydro["schema"] == "geohydroai.hydrograph/v1" and len(hydro["p50"]) == 216 and hydro["members_n"] == 2
    assert hydro["peak"]["p95_m3s"] >= hydro["peak"]["p50_m3s"] >= hydro["peak"]["p5_m3s"] > 0
    assert hydro["q_by_member"] is not None and len(hydro["q_by_member"]) == 2
    assert (tmp_path / "out" / "result.json").exists()


def test_daily_child_products_say_gr4j_and_its_limits(tmp_path, monkeypatch):
    from lab_service.gr4h.validate import prepare_gr4h
    _fake_rscript(monkeypatch)
    p = prepare_gr4h(_params(time_step="1d", forcing=_daily(), event={"start": "2020-01-20T00:00:00Z", "end": "2020-01-29T00:00:00Z"}))
    events = _run_child(tmp_path, p)
    assert events[-1][0] == "done", events[-1]
    r = events[-1][1]["result"]
    assert r["stats"]["time_step"] == "1d" and r["stats"]["steps"] == 10 and r["stats"]["hours"] == 240
    hydro = json.loads(__import__("base64").b64decode(events[-1][1]["products"][0]["content_b64"]))
    assert hydro["engine"] == "airgr-gr4j" and hydro["model"] == "CemaNeigeGR4J" and hydro["time_step"] == "1d"
    assert hydro["time_utc"][0] == "2020-01-20T00:00:00Z" and hydro["time_utc"][1] == "2020-01-21T00:00:00Z"
    assert "first model type" in hydro["statement"] and any("daily step" in x for x in hydro["limitations"])
    assert any("2 vCPU" in x for x in hydro["limitations"])
    # mm/day → m³/s over 552 km²: 0.01 mm/d is 0.0639 m³/s
    assert abs(hydro["reference"][0] - 0.01 * 552e6 / 86400 / 1000) < 1e-3
    assert r["metadata"]["model"] == "CemaNeigeGR4J" and r["metadata"]["warmup"]["steps"] == 390


def test_child_reports_r_failure(tmp_path, monkeypatch):
    import subprocess

    from lab_service.gr4h.validate import prepare_gr4h
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "Error in x : boom\nExecution halted"))
    events = _run_child(tmp_path, prepare_gr4h(_params()))
    assert events[-1][0] == "failed" and "airGR run failed" in events[-1][1] and "boom" in events[-1][1]


@pytest.mark.skipif(shutil.which("Rscript") is None, reason="R / airGR not installed (the lab image has them)")
def test_real_airgr_reference_run(tmp_path):
    from lab_service.gr4h.validate import prepare_gr4h
    f = _forcing(days=400)
    f["p_mm"] = [0.0] * len(f["p_mm"])
    for h in range(24 * 384, 24 * 385):      # 20 mm/h for a day at the start of the event
        f["p_mm"][h] = 20.0
    events = _run_child(tmp_path, prepare_gr4h(_params(forcing=f)))
    assert events[-1][0] == "done", events[-1]
    r = events[-1][1]["result"]
    assert r["stats"]["peak"]["reference_m3s"] > 1.0 and r["metadata"]["engine_version"] == "1.7.9"


@pytest.mark.skipif(shutil.which("Rscript") is None, reason="R / airGR not installed (the lab image has them)")
def test_real_airgr_daily_run(tmp_path):
    from lab_service.gr4h.validate import prepare_gr4h
    f = _daily(days=400)
    f["p_mm"] = [1.0] * 400
    f["p_mm"][384] = 80.0                    # 80 mm on the first day of the event
    events = _run_child(tmp_path, prepare_gr4h(_params(time_step="1d", forcing=f, event={"start": "2020-01-20T00:00:00Z", "end": "2020-02-03T00:00:00Z"})))
    assert events[-1][0] == "done", events[-1]
    r = events[-1][1]["result"]
    assert r["metadata"]["model"] == "CemaNeigeGR4J" and r["stats"]["peak"]["reference_m3s"] > 1.0
