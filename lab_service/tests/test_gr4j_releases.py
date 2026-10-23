"""Daily CemaNeige-GR4J parameter releases (lab_service/gr4h/params/releases.json): the files load and validate, the
owner's per-gauge decisions of 2026-10-22, release selection and rollback, the ensemble without the precipitation ×1.25
members, publish flags, and model_version in every run's metadata and products."""
from __future__ import annotations

import base64
import json
import math
import shutil

import pytest

from lab_service import settings
from lab_service import validate as V
from lab_service.gr4h import validate as GV
from lab_service.tests.test_gr4h import _daily, _fake_rscript, _params, _run_child

OLD = "prut-network-gr4j-climuad-2026-10-16"
NEW = "gr4j-era5land-2026.10"
NEW_DEFAULT = ["42136", "42137", "42148", "42187", "42194"]      # improvement confirmed, published
CONTROL = ["42198", "42202"]                                       # old sets stay; ERA5-Land sets as candidate
UNRELIABLE = ["42176", "42201", "42249"]                          # runnable for the network, never published
TEN = sorted(NEW_DEFAULT + CONTROL + UNRELIABLE)


@pytest.fixture(autouse=True)
def _no_env_release(monkeypatch):
    monkeypatch.setattr(settings, "GR4J_PARAMS_RELEASE", "")


def _daily_params(**over):
    base = _params(time_step="1d", forcing=_daily(), event={"start": "2020-01-20T00:00:00Z", "end": "2020-01-29T00:00:00Z"})
    base.update(over)
    return base


def _all_files():
    m = GV.releases()
    for rid, rel in m["releases"].items():
        for post, e in rel["posts"].items():
            for f in filter(None, (e["file"], e.get("candidate"))):
                yield rid, post, f


# ---- files ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("rid, post, fname", list(_all_files()))
def test_params_file_loads_and_validates(rid, post, fname):
    d = json.loads((GV.PARAMS_DIR / fname).read_text())
    assert d["schema"] == "geohydroai.gr4h_params/v1" and d["model"] == "CemaNeigeGR4J" and d["time_step"] == "1d"
    assert d["post_id"] == post and d["version"]
    ids = [s["id"] for s in d["sets"]]
    assert ids[0] == "reference" and len(ids) == len(set(ids)) >= 4
    for s in d["sets"]:
        x = [s[k] for k in ("X1", "X2", "X3", "X4", "CNX1", "CNX2")]
        assert all(isinstance(v, (int, float)) and math.isfinite(v) for v in x)
        assert s["X1"] > 0 and s["X3"] > 0 and s["X4"] >= 0.5 and 0 <= s["CNX1"] <= 1 and s["CNX2"] >= 0
    if d["version"] == NEW:
        cal = d["calibration"]
        assert "ERA5-Land daily_climuad" in cal["forcing"] and "20:00→20:00 Kyiv" in cal["forcing"]
        assert cal["objective"].startswith("0.5·KGE′(Q) + 0.5·KGE′(1/(Q+ε))")
        assert cal["periods"]["reference"] == "1981-2023" and cal["periods"]["split_sample"] == ["1981-2001", "2002-2023"]
        assert {"validation", "current_same_forcing", "2021_2023"} <= set(d["skill"])
        for s in d["sets"]:
            assert s["calibration"]["period"] and s["calibration"]["objective"] == cal["objective"]
            assert s["calibration"]["forcing"] == cal["forcing"]
            if s["id"].startswith("C_S"):
                assert s["calibration"]["validation"] and s["calibration"]["kge2_val"] is not None


def test_every_new_file_is_in_its_own_release_folder():
    files = sorted(p.name for p in (GV.PARAMS_DIR / NEW).glob("*.1d.json"))
    assert files == [f"{p}.1d.json" for p in TEN]
    for f in files:
        assert json.loads((GV.PARAMS_DIR / NEW / f).read_text())["version"] == NEW


# ---- the owner's decisions (2026-10-22) -----------------------------------------------------------------------------
def test_manifest_keeps_the_operational_release_as_default():
    m = GV.releases()
    assert m["default"] == OLD and m["releases"][OLD]["status"] == "operational"
    assert m["releases"][NEW]["status"] == "candidate" and m["releases"][NEW]["previous"] == OLD


def test_release_table_per_gauge():
    posts = GV.releases()["releases"][NEW]["posts"]
    for p in NEW_DEFAULT:
        assert posts[p]["file"] == f"{NEW}/{p}.1d.json" and posts[p]["publish"] is True and "candidate" not in posts[p]
    for p in CONTROL:
        assert posts[p]["file"] == f"{p}.1d.json" and posts[p]["candidate"] == f"{NEW}/{p}.1d.json" and posts[p]["publish"] is True
        assert json.loads((GV.PARAMS_DIR / posts[p]["file"]).read_text())["version"] == OLD
    for p in UNRELIABLE:
        assert posts[p]["publish"] is False and posts[p]["quality"] == "unreliable"
        assert posts[p]["file"] == f"{NEW}/{p}.1d.json"            # runnable: the network water balance needs them
    # never published as a reliable forecast, whatever the release
    for p in UNRELIABLE:
        assert GV.releases()["releases"][OLD]["posts"][p]["publish"] is False


def test_old_release_files_are_untouched():
    for post, e in GV.releases()["releases"][OLD]["posts"].items():
        assert e["file"] == f"{post}.1d.json" and "candidate" not in e
        assert json.loads((GV.PARAMS_DIR / e["file"]).read_text())["version"] == OLD


# ---- selection and rollback -----------------------------------------------------------------------------------------
def test_default_is_the_operational_release():
    p = GV.prepare_gr4h(_daily_params())
    assert p["params_release"] == OLD and p["model_version"] == OLD and p["parameters"]["release"] == OLD
    assert [s["id"] for s in p["parameters"]["sets"]][0] == "reference" and len(p["parameters"]["sets"]) == 4


def test_switch_and_rollback_by_setting(monkeypatch):
    monkeypatch.setattr(settings, "GR4J_PARAMS_RELEASE", NEW)
    p = GV.prepare_gr4h(_daily_params())
    assert p["params_release"] == NEW and p["model_version"] == NEW and len(p["parameters"]["sets"]) == 6
    assert p["parameters"]["file"] == f"{NEW}/42194.1d.json" and p["parameters"]["publish"] is True
    monkeypatch.setattr(settings, "GR4J_PARAMS_RELEASE", OLD)               # rollback
    q = GV.prepare_gr4h(_daily_params())
    assert q["params_release"] == OLD and q["model_version"] == OLD and len(q["parameters"]["sets"]) == 4
    monkeypatch.setattr(settings, "GR4J_PARAMS_RELEASE", "")                 # cleared = the manifest default
    assert GV.prepare_gr4h(_daily_params())["model_version"] == OLD


def test_job_can_name_a_release_and_the_control_candidate(monkeypatch):
    p = GV.prepare_gr4h(_daily_params(params_release=NEW))
    assert p["model_version"] == NEW
    c = GV.prepare_gr4h(_daily_params(post_id="42198", params_release=NEW))
    assert c["model_version"] == OLD and c["parameters"]["variant"] == "default"      # control keeps the ClimUAd sets
    k = GV.prepare_gr4h(_daily_params(post_id="42198", params_release=NEW, param_variant="candidate"))
    assert k["model_version"] == NEW and k["parameters"]["variant"] == "candidate" and k["parameters"]["file"] == f"{NEW}/42198.1d.json"
    with pytest.raises(V.Rejected, match="no candidate"):
        GV.prepare_gr4h(_daily_params(params_release=NEW, param_variant="candidate"))   # 42194 has none
    with pytest.raises(V.Rejected, match="param_variant"):
        GV.prepare_gr4h(_daily_params(param_variant="other"))


def test_unknown_release_is_rejected(monkeypatch):
    with pytest.raises(V.Rejected, match="unknown GR4J parameter release"):
        GV.prepare_gr4h(_daily_params(params_release="nope"))
    monkeypatch.setattr(settings, "GR4J_PARAMS_RELEASE", "typo")
    with pytest.raises(V.Rejected, match="unknown GR4J parameter release"):
        GV.prepare_gr4h(_daily_params())


def test_unreliable_gauges_stay_runnable_but_unpublished():
    for post in UNRELIABLE:
        p = GV.prepare_gr4h(_daily_params(post_id=post, params_release=NEW))
        assert p["parameters"]["publish"] is False and p["parameters"]["quality"] == "unreliable" and p["model_version"] == NEW


def test_hourly_sets_are_not_part_of_a_release():
    assert GV.published_posts("1h") == ["42194"]                              # releases.json is not a gauge
    p = GV.prepare_gr4h(_params())
    assert p["params_release"] is None and p["model_version"] == "prut-network-2026-10-17"
    assert GV.published_posts("1d", NEW) == sorted(TEN + ["42130"]) == GV.published_posts("1d")


# ---- ensemble: no precipitation ×1.25 -------------------------------------------------------------------------------
def test_new_release_ensemble_is_parameter_sets_only():
    p = GV.prepare_gr4h(_daily_params(params_release=NEW, members={"mode": "ensemble"}))
    assert p["members"]["depth_factors"] == [1.0] and p["members"]["param_sets"] == "behavioural"
    assert 1.25 not in p["members"]["depth_factors"]


def test_old_release_ensemble_is_unchanged_for_rollback():
    p = GV.prepare_gr4h(_daily_params(members={"mode": "ensemble"}))
    assert p["members"]["depth_factors"] == [1.0, 1.25]


# ---- run metadata (fake Rscript) ------------------------------------------------------------------------------------
def _products(events):
    return {x["kind"]: json.loads(base64.b64decode(x["content_b64"])) for x in events[-1][1]["products"]}, events[-1][1]


@pytest.mark.parametrize("post, release, version, publish", [
    ("42194", NEW, NEW, True), ("42194", OLD, OLD, True), ("42249", NEW, NEW, False), ("42198", NEW, OLD, True)])
def test_run_carries_model_version_and_publish_flag(tmp_path, monkeypatch, post, release, version, publish):
    _fake_rscript(monkeypatch)
    events = _run_child(tmp_path, GV.prepare_gr4h(_daily_params(post_id=post, params_release=release)))
    assert events[-1][0] == "done", events[-1]
    prods, payload = _products(events)
    meta, hydro, stats = payload["result"]["metadata"], prods["hydrograph"], payload["result"]["stats"]
    for obj in (meta, hydro, stats, prods["model_metadata"]):
        assert obj["model_version"] == version and obj["params_release"] == release and obj["publish"] is publish
    for x in payload["products"]:
        assert x["stats"]["model_version"] == version
    assert meta["parameters"]["version"] == version and meta["parameters"]["release"] == release
    assert meta["parameters"]["set_ids"][0] == "reference"
    if version == NEW:
        assert "ERA5-Land" in meta["parameters"]["calibration"]["forcing"] and meta["parameters"]["skill"]
    unpublished = any("not published as a forecast" in x for x in hydro["limitations"])
    assert unpublished is (not publish)


def test_inline_parameters_say_inline(tmp_path, monkeypatch):
    _fake_rscript(monkeypatch)
    sets = json.loads((GV.PARAMS_DIR / "42194.1d.json").read_text())["sets"][:1]
    p = GV.prepare_gr4h(_daily_params(post_id=None, parameters={"sets": sets}))
    assert p["model_version"] == "inline"
    events = _run_child(tmp_path, p)
    assert events[-1][1]["result"]["metadata"]["model_version"] == "inline"


# ---- the real airGR run of every new set (lab image / geohydroai-gr4h) --------------------------------------------
@pytest.mark.skipif(shutil.which("Rscript") is None, reason="R / airGR not installed (the lab image has them)")
@pytest.mark.parametrize("post, variant", [(p, "default") for p in TEN] + [(p, "candidate") for p in CONTROL])
def test_real_airgr_new_release_ensemble(tmp_path, post, variant):
    f = _daily(days=400)
    f["p_mm"] = [1.5] * 400
    f["p_mm"][384] = 60.0
    p = GV.prepare_gr4h(_daily_params(post_id=post, forcing=f, params_release=NEW, param_variant=variant,
                                      event={"start": "2020-01-20T00:00:00Z", "end": "2020-02-03T00:00:00Z"},
                                      members={"mode": "ensemble"}))
    events = _run_child(tmp_path, p)
    assert events[-1][0] == "done", events[-1]
    prods, payload = _products(events)
    hydro = prods["hydrograph"]
    n_sets = len(p["parameters"]["sets"])
    assert hydro["members_n"] == n_sets and {m["depth_factor"] for m in hydro["members"]} == {1.0}
    assert all(math.isfinite(v) and v >= 0 for row in hydro["q_by_member"] for v in row)
    assert payload["result"]["metadata"]["model_version"] == p["model_version"]
