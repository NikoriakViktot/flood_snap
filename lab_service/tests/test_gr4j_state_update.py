"""The morning state update of a daily CemaNeige-GR4J run from an observed discharge (state_update, PR 3 of the forecast
rollout): request validation (no look-ahead, daily only, inside the event), the products with both variants (fake
Rscript), and the real airGR update (lab image)."""
from __future__ import annotations

import base64
import csv
import json
import shutil
import subprocess
from pathlib import Path

import jsonschema
import numpy as np
import pytest

from lab_service import validate as V
from lab_service.gr4h import validate as GV
from lab_service.tests.test_gr4h import INPUT, _daily, _fake_rscript, _params, _run_child

SU = {"day": "2020-01-25", "q_m3s": 12.0, "observed_at": "2020-01-25T06:00:00Z", "issued_at": "2020-01-25T07:30:00Z",
      "source": "UkrHMC bulletin 2020-01-25 08:00", "provenance": {"level_cm": 143, "rating": "TG-10 2023"}}


def _dp(**over):
    base = _params(time_step="1d", forcing=_daily(), event={"start": "2020-01-20T00:00:00Z", "end": "2020-01-29T00:00:00Z"})
    base.update(over)
    return base


def _products(events):
    return {x["kind"]: json.loads(base64.b64decode(x["content_b64"])) for x in events[-1][1]["products"]}, events[-1][1]


# ---- validation ---------------------------------------------------------------------------------------------------------
def test_state_update_is_normalised_and_in_the_contract():
    raw = _dp(state_update=SU)
    jsonschema.validate({"job_id": "j1", "params": raw}, INPUT)
    p = GV.prepare_gr4h(raw)
    su = p["state_update"]
    assert su["day"] == "2020-01-25" and su["use"] == "shadow" and su["max_store_change_frac"] == 1.0
    assert su["q_mm"] == pytest.approx(12.0 * 86.4 / 552.0, rel=1e-5)          # m³/s → mm/day over the basin
    assert su["provenance"] == {"level_cm": 143, "rating": "TG-10 2023"} and su["issued_at"] == "2020-01-25T07:30:00Z"
    assert GV.prepare_gr4h(_dp())["state_update"] is None


@pytest.mark.parametrize("over, msg", [
    ({"q_m3s": 0}, "positive discharge"),
    ({"q_m3s": "x"}, "must be a number"),
    ({"day": "2020-02-10"}, "inside the event window"),
    ({"day": "2020-01-19"}, "inside the event window"),
    ({"observed_at": "2020-01-25T09:00:00Z"}, "no look-ahead"),
    ({"use": "always"}, "state_update.use"),
    ({"max_store_change_frac": 5}, "max_store_change_frac"),
])
def test_state_update_rejects(over, msg):
    with pytest.raises(V.Rejected, match=msg):
        GV.prepare_gr4h(_dp(state_update={**SU, **over}))


def test_state_update_is_daily_only():
    with pytest.raises(V.Rejected, match="daily"):
        GV.prepare_gr4h(_params(state_update=SU))


# ---- products (fake Rscript) ----------------------------------------------------------------------------------------------
def _fake(monkeypatch, status="matched"):
    """run.R stand-in: open loop 0.01 mm/day rising, the updated series ×2 from the update day on."""
    def run(cmd, **kw):
        wd = Path(cmd[-1])
        cfg = json.loads((wd / "config.json").read_text())
        with (wd / "forcing.csv").open() as fh:
            times = [r["time_utc"] for r in csv.DictReader(fh)]
        t0, t1 = cfg["run_start"].replace(" ", "T") + "Z", cfg["run_end"].replace(" ", "T") + "Z"
        run_t = [t for t in times if t0 <= t <= t1]
        members = ["m0000", "m0001"]
        ol = {m: [round(0.5 + 0.1 * i + k, 5) for i in range(len(run_t))] for k, m in enumerate(members)}
        for name, q in (("qsim.csv", ol), ("qsim_updated.csv", {m: [v * (2 if t[:10] >= cfg["state_update"]["day"] else 1)
                                                                    for t, v in zip(run_t, ol[m], strict=True)] for m in members})):
            with (wd / name).open("w", newline="") as fh:
                w = csv.writer(fh); w.writerow(["time_utc", *members])
                for i, t in enumerate(run_t):
                    w.writerow([t, *[q[m][i] for m in members]])
        with (wd / "members.csv").open("w", newline="") as fh:
            w = csv.writer(fh); w.writerow(["member", "set", "hyeto", "alpha", "depth"])
            for m in members:
                w.writerow([m, f"s{m[-1]}", "even", "NA", 1.0])
        with (wd / "state_update.csv").open("w", newline="") as fh:
            w = csv.writer(fh); w.writerow(["member", "R_open_loop_mm", "R_updated_mm", "dR_mm", "q_obs_mm", "q_open_loop_mm", "q_updated_mm", "status"])
            w.writerow(["m0000", 40.0, 55.0, 15.0, cfg["state_update"]["q_mm"], 1.0, cfg["state_update"]["q_mm"], "matched"])
            w.writerow(["m0001", 40.0, 200.0, 160.0, cfg["state_update"]["q_mm"], 2.0, 2.0, status])
        (wd / "state.json").write_text(json.dumps({"schema": "geohydroai.gr4h_state/v1", "states": {}}))
        (wd / "versions.json").write_text(json.dumps({"R": "R 4.1.2", "airGR": "1.7.9", "members": 2, "pet": "Oudin"}))
        return subprocess.CompletedProcess(cmd, 0, "ok", "")
    monkeypatch.setattr(subprocess, "run", run)


@pytest.mark.parametrize("use, primary", [("shadow", "open_loop"), ("primary", "updated")])
def test_products_carry_both_variants(tmp_path, monkeypatch, use, primary):
    _fake(monkeypatch, status="rejected")
    p = GV.prepare_gr4h(_dp(state_update={**SU, "use": use}, members={"mode": "ensemble"}))
    events = _run_child(tmp_path, p)
    assert events[-1][0] == "done", events[-1]
    prods, payload = _products(events)
    h = prods["hydrograph"]
    b = h["state_update"]
    assert b["primary"] == primary and b["day"] == "2020-01-25" and b["observed_at"] == "2020-01-25T06:00:00Z"
    assert b["status_counts"] == {"matched": 1, "rejected": 1} and b["applied_members"] == 1 and b["dR_mm_median"] == 15.0
    assert b["provenance"]["level_cm"] == 143
    m0 = b["members"][0]
    assert m0["dR_m3"] == round(15.0 / 1000 * 552.0 * 1e6) and m0["q_obs_m3s"] == pytest.approx(12.0, abs=1e-3)
    # the main series are the primary variant; the other is in the block
    assert h["reference"] == b[primary]["reference"] and h["p50"] == b[primary]["p50"]
    i = h["time_utc"].index("2020-01-25T00:00:00Z")
    assert b["updated"]["reference"][i] == pytest.approx(2 * b["open_loop"]["reference"][i])
    assert b["updated"]["reference"][i - 1] == b["open_loop"]["reference"][i - 1]
    meta = payload["result"]["metadata"]
    assert meta["state_update"]["primary"] == primary and "open_loop" not in meta["state_update"]
    assert payload["result"]["stats"]["state_update"] == primary
    # the update is part of the run's identity
    p2 = GV.prepare_gr4h(_dp(members={"mode": "ensemble"}))
    _fake_rscript(monkeypatch)
    assert _run_child(tmp_path / "b", p2)[-1][1]["result"]["config_hash"] != payload["result"]["config_hash"]


# ---- the real airGR update (lab image) ------------------------------------------------------------------------------------
def _real(tmp_path, **su):
    f = _daily(days=400)
    f["p_mm"] = [1.5] * 400
    f["p_mm"][370] = 45.0
    p = GV.prepare_gr4h(_dp(forcing=f, params_release="gr4j-era5land-2026.10", members={"mode": "ensemble"},
                            event={"start": "2020-01-20T00:00:00Z", "end": "2020-01-31T00:00:00Z"}, state_update={**SU, **su}))
    events = _run_child(tmp_path, p)
    assert events[-1][0] == "done", events[-1]
    return _products(events)[0]["hydrograph"]


@pytest.mark.skipif(shutil.which("Rscript") is None, reason="R / airGR not installed (the lab image has them)")
@pytest.mark.parametrize("q_obs", [3.0, 25.0])
def test_real_update_matches_the_observation(tmp_path, q_obs):
    h = _real(tmp_path, q_m3s=q_obs, use="primary")
    b = h["state_update"]
    i = h["time_utc"].index("2020-01-25T00:00:00Z")
    assert b["primary"] == "updated" and len(b["members"]) == h["members_n"]
    for m in b["members"]:
        assert m["status"] in ("matched", "bounded_low", "bounded_high", "rejected")
        if m["status"] == "matched":
            assert m["q_updated_m3s"] == pytest.approx(q_obs, rel=1e-3)
            assert abs(m["dR_mm"]) <= m["R_open_loop_mm"] + 1e3
    assert any(m["status"] == "matched" for m in b["members"])
    # before the update day nothing changes; from it on the updated series start at the observation
    assert b["updated"]["reference"][:i] == b["open_loop"]["reference"][:i]
    ref = b["members"][0]
    if ref["status"] == "matched":
        assert b["updated"]["reference"][i] == pytest.approx(q_obs, rel=1e-2)
    # all series finite and non-negative
    for v in ("open_loop", "updated"):
        assert all(np.isfinite(x) and x >= 0 for x in b[v]["reference"] + b[v]["p50"])


@pytest.mark.skipif(shutil.which("Rscript") is None, reason="R / airGR not installed (the lab image has them)")
def test_real_update_refuses_an_implausible_store_change(tmp_path):
    h = _real(tmp_path, q_m3s=900.0, max_store_change_frac=0.05)     # far above anything the stores can give
    b = h["state_update"]
    assert b["primary"] == "open_loop"
    assert all(m["status"] in ("rejected", "bounded_high") for m in b["members"])
    for m in b["members"]:
        if m["status"] == "rejected":
            assert m["q_updated_m3s"] == m["q_open_loop_m3s"]
