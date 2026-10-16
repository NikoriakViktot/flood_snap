"""Synthetic rating curves (hand/src.py) against a brute-force evaluation of the FIM formulas."""
from __future__ import annotations

import math

import numpy as np
import pytest

from lab_service import settings
from lab_service.hand import src as SRC

NODATA = -9999.0
RES = 30.0


def valley(rows=41, cols=50, side=0.5):
    """one catchment (id 7): HAND = side·|r − mid|, flat cells (slope 0)"""
    r = np.arange(rows)[:, None]
    hand = np.repeat(side * np.abs(r - rows // 2), cols, axis=1).astype(np.float32)
    catch = np.full(hand.shape, 7, np.int32)
    slope = np.zeros(hand.shape, np.float32)
    return hand, catch, slope


def brute(hand, slope_deg, stage, length, s, n):
    wet = hand < stage
    a = RES * RES
    vol = ((stage - hand)[wet]).sum() * a
    bed = (1 / np.cos(np.radians(slope_deg[wet]))).sum() * a
    area, perim = vol / length, bed / length
    radius = area / perim if perim else 0.0
    return area * radius ** (2 / 3) * math.sqrt(s) / n if stage > 0 else 0.0


def test_discharge_matches_the_formulas():
    hand, catch, slope = valley()
    reach = {"id": 7, "to": 0, "length_m": 50 * RES, "slope": 0.005, "drainage_area_km2": 10.0}
    t = SRC.rating_curves(hand, catch, slope, [reach], RES, NODATA)
    row = t["reaches"][0]
    assert t["schema"] == "geohydroai.hydrotable/v1" and row["short"] is False and row["src_from"] is None
    for i, h in enumerate(t["stages_m"]):
        exp = brute(hand, slope, h, 50 * RES, 0.005, settings.SRC_MANNING_N)
        assert row["discharge_m3s"][i] == pytest.approx(exp, rel=1e-3, abs=1e-6), h
    q = np.array(row["discharge_m3s"])
    assert q[0] == 0 and (np.diff(q) >= 0).all()
    # top width at 2 m: HAND < 2 → |r − 20| < 4 → 7 rows of 30 m
    i2 = t["stages_m"].index(2.0)
    assert row["top_width_m"][i2] == pytest.approx(7 * RES)


def test_slope_is_floored_and_steep_cells_widen_the_bed():
    hand, catch, slope = valley()
    flat = SRC.rating_curves(hand, catch, slope, [{"id": 7, "length_m": 1500, "slope": 0.0}], RES, NODATA)["reaches"][0]
    assert flat["slope"] == settings.SRC_SLOPE_MIN and flat["slope_source"] == "slope_min"
    steep = SRC.rating_curves(hand, catch, np.full(hand.shape, 30, np.float32), [{"id": 7, "length_m": 1500, "slope": 0.0}],
                              RES, NODATA)["reaches"][0]
    i = 8
    assert steep["hydraulic_radius_m"][i] < flat["hydraulic_radius_m"][i]       # longer wetted perimeter
    assert steep["discharge_m3s"][i] < flat["discharge_m3s"][i]


def test_short_reach_takes_the_curve_downstream():
    hand, catch, slope = valley(rows=41, cols=50)
    catch[:, :1] = 3                                     # reach 3: one column (30 m, 0.04 km²) draining into 7
    reaches = [{"id": 3, "to": 7, "length_m": 30, "slope": 0.01}, {"id": 7, "to": 0, "length_m": 49 * RES, "slope": 0.005}]
    t = {r["id"]: r for r in SRC.rating_curves(hand, catch, slope, reaches, RES, NODATA)["reaches"]}
    assert t[3]["short"] is True and t[3]["src_from"] == 7
    assert t[3]["discharge_m3s"] == t[7]["discharge_m3s"]


def test_stage_for_interpolates_and_clamps():
    hand, catch, slope = valley()
    t = SRC.rating_curves(hand, catch, slope, [{"id": 7, "length_m": 1500, "slope": 0.005}], RES, NODATA)
    row, st = t["reaches"][0], t["stages_m"]
    q = row["discharge_m3s"]
    assert SRC.stage_for(row, st, q[10]) == pytest.approx(st[10])
    assert st[10] < SRC.stage_for(row, st, (q[10] + q[11]) / 2) < st[11]
    assert SRC.stage_for(row, st, 1e12) == st[-1]
    assert SRC.stage_for(row, st, 0) == 0


def test_nodata_and_foreign_catchments_are_ignored():
    hand, catch, slope = valley()
    hand[:5, :] = NODATA
    catch[:, -5:] = 0
    t = SRC.rating_curves(hand, catch, slope, [{"id": 7, "length_m": 1500, "slope": 0.005}, {"id": 99, "length_m": 900, "slope": 0.01}],
                          RES, NODATA)
    assert [r["id"] for r in t["reaches"]] == [7]
    assert t["reaches"][0]["catchment_km2"] == pytest.approx((41 - 5) * 45 * RES * RES / 1e6, rel=1e-6)


def test_an_oversized_hydrotable_keeps_only_discharge(monkeypatch):
    from lab_service.hand import products as PR
    hand, catch, slope = valley()
    t = SRC.rating_curves(hand, catch, slope, [{"id": 7, "length_m": 1500, "slope": 0.005}], RES, NODATA)
    slim = PR.slim_hydrotable(t)
    assert slim["slim"] is True and "discharge_m3s" in slim["reaches"][0] and "top_width_m" not in slim["reaches"][0]
    assert t["reaches"][0]["top_width_m"]                           # the original is untouched
