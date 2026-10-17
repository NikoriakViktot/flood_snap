"""Discharge → stage per reach on a HAND model's synthetic rating curves (hydrotable, geohydroai.hydrotable/v1).

The same rule as the browser (geoai-frontend-v2 src/domain/hydrotable.ts): the discharge enters at the outlet reach (the
largest drainage area) and moves to every reach as Q_reach = Q_ref · (A_reach / A_ref)^b, b = 0.8 (an assumption, shown
to the user); the stage is the linear, end-clamped interpolation of the reach's curve (hand/src.py stage_for); reaches
outside 0.5–2× the reference area are low-confidence transfers. Pure Python so the server and the browser agree."""
from __future__ import annotations

import numpy as np

AREA_EXPONENT = 0.8
PERCENTILES = (5, 50, 95)


def stage_for(reach: dict, stages_m: list[float], q_m3s: float) -> float:
    q = reach["discharge_m3s"]
    n = min(len(q), len(stages_m))
    if n == 0 or not np.isfinite(q_m3s):
        return 0.0
    return float(np.interp(q_m3s, q[:n], stages_m[:n]))


def q_transfer(q_ref: float, area_ref_km2: float, area_reach_km2: float, b: float = AREA_EXPONENT) -> float:
    if not (q_ref > 0) or not (area_ref_km2 > 0) or not (area_reach_km2 > 0):
        return 0.0
    return q_ref * (area_reach_km2 / area_ref_km2) ** b


def transfer_confidence(area_ref_km2: float, area_reach_km2: float) -> str:
    r = area_reach_km2 / area_ref_km2 if area_ref_km2 > 0 else 0
    return "high" if 0.5 <= r <= 2 else "low"


def outlet_area(hydrotable: dict) -> float | None:
    best = None
    for r in hydrotable["reaches"]:
        a = r.get("drainage_area_km2")
        if a is not None and (best is None or a > best):
            best = a
    return best


def stage_table(hydrotable: dict, q_ref: float, area_ref_km2: float, b: float = AREA_EXPONENT) -> dict[int, float]:
    """Stage (m) by reach id for one reference discharge; reaches without a curve or area stay dry."""
    stages = hydrotable["stages_m"]
    out: dict[int, float] = {}
    for r in hydrotable["reaches"]:
        a = r.get("drainage_area_km2")
        if not a or not r.get("discharge_m3s"):
            continue
        out[int(r["id"])] = stage_for(r, stages, q_transfer(q_ref, area_ref_km2, a, b))
    return out


def percentiles(q_by_member: np.ndarray, pcts=PERCENTILES) -> dict[str, np.ndarray]:
    """q_by_member: members × steps → {"p5": steps, …} (nearest-rank free: numpy linear interpolation)."""
    return {f"p{p}": np.percentile(q_by_member, p, axis=0) for p in pcts}


def stage_table_product(hydrotable: dict, time_utc: list[str], q_by_member: np.ndarray, members: list[dict],
                        area_ref_km2: float | None = None) -> dict:
    """geohydroai.stage_table/v1: per output step the p5/p50/p95 discharge and, for every reach, the stage of each
    percentile; plus the peak step (of p50) in full. Q enters at the outlet reach; `low_confidence` lists the reaches
    whose drainage area is outside 0.5–2× the reference."""
    area_ref = area_ref_km2 or outlet_area(hydrotable)
    if not area_ref:
        raise ValueError("the hydrotable has no reach with a drainage area: no reference for the transfer")
    reaches = [r for r in hydrotable["reaches"] if r.get("drainage_area_km2") and r.get("discharge_m3s")]
    ids = [int(r["id"]) for r in reaches]
    pct = percentiles(q_by_member)
    peak_i = int(np.argmax(pct["p50"]))
    steps = []
    for i, t in enumerate(time_utc):
        row = {"t": t, "q_m3s": {k: round(float(v[i]), 3) for k, v in pct.items()}}
        if i == peak_i:
            row["stages_m"] = {k: [round(stage_for(r, hydrotable["stages_m"], q_transfer(float(v[i]), area_ref, r["drainage_area_km2"])), 3)
                                   for r in reaches] for k, v in pct.items()}
        steps.append(row)
    # the per-step stage of the p50 only (compact): one number per reach and step
    p50 = pct["p50"]
    stage_p50 = [[round(stage_for(r, hydrotable["stages_m"], q_transfer(float(p50[i]), area_ref, r["drainage_area_km2"])), 2)
                  for r in reaches] for i in range(len(time_utc))]
    exceed = None
    if q_by_member.shape[0] > 1:
        # at the peak step: share of members whose stage on the reach exceeds each 0.5 m threshold → a cheap probability
        qs = q_by_member[:, peak_i]
        exceed = {"thresholds_m": [0.5, 1, 2, 3], "share_by_reach": []}
        for r in reaches:
            st = np.array([stage_for(r, hydrotable["stages_m"], q_transfer(float(q), area_ref, r["drainage_area_km2"])) for q in qs])
            exceed["share_by_reach"].append([round(float((st > th).mean()), 3) for th in exceed["thresholds_m"]])
    low = [i for i, r in zip(ids, reaches) if transfer_confidence(area_ref, r["drainage_area_km2"]) == "low"]
    return {"schema": "geohydroai.stage_table/v1", "reach_ids": ids, "area_ref_km2": area_ref, "area_exponent": AREA_EXPONENT,
            "stages_unit": "m above the reach's HAND datum", "members": len(members), "percentiles": list(PERCENTILES),
            "peak_step": peak_i, "steps": steps, "stage_p50_by_step": stage_p50, "exceedance_at_peak": exceed,
            "low_confidence_reaches": low,
            "rule": "cell wet when HAND < stage of its reach; depth = stage − HAND (FIM); screening estimate, not a hydraulic model"}
