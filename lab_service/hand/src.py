"""Synthetic rating curves (SRC) per reach: discharge Q(stage) from the HAND model, the FIM approach.

For every reach (a WhiteboxTools stream link) and every stage h on a fixed ladder, the cells of the reach's catchment with
HAND < h are flooded (Nobre 2011, NOAA OWP FIM):
    cells N(h)            surface area   Sa = N·a                     (a = cell area)
    volume V(h) = Σ (h − HAND)·a          bed area      Ba = Σ a·√(1 + tan²β)   (β = cell slope)
and, divided by the reach length L, the reach-averaged cross-section:
    top width = Sa/L   wet area A = V/L   wetted perimeter P = Ba/L   hydraulic radius R = A/P
    Manning:  Q = A · R^(2/3) · S^(1/2) / n           (S = reach slope ≥ SRC_SLOPE_MIN, n = SRC_MANNING_N)

The per-stage geometry re-implements what TauDEM `catchhydrogeo` computes for FIM (its documented behaviour; no TauDEM
code is used). The hydraulics follow NOAA OWP inundation-mapping `src/add_crosswalk.py` (v4.10, Apache-2.0,
https://github.com/NOAA-OWP/inundation-mapping): same formulas, same defaults (n = 0.06, S_min = 0.001). Short reaches
(FIM `small_segments`: catchment < SRC_MIN_CATCHMENT_KM2 and length < SRC_MIN_LENGTH_M) take the curve of the reach
downstream, as their own geometry divides a catchment by a length of a cell or two.

This is a hydrologic screening rating curve, not a hydraulic model: no bathymetry below the DEM water surface, one
roughness for channel and floodplain, uniform flow. It turns a discharge into a stage per reach (the stage is then
spread over the reach catchment by HAND: depth = stage − HAND)."""
from __future__ import annotations

import math

import numpy as np

from lab_service import settings

SCHEMA = "geohydroai.hydrotable/v1"


def stage_ladder(step: float | None = None, top: float | None = None) -> np.ndarray:
    step = settings.SRC_STAGE_STEP_M if step is None else step
    top = settings.SRC_STAGE_MAX_M if top is None else top
    return np.round(np.arange(0.0, top + step / 2, step), 4)


def _geometry(h: np.ndarray, bed: np.ndarray, stages: np.ndarray, cell_area: float):
    """Cells, volume and bed area under every stage for one catchment (HAND values h, bed factors √(1+tan²β))."""
    order = np.argsort(h, kind="stable")
    hs = h[order].astype(np.float64)
    cum_h = np.concatenate([[0.0], np.cumsum(hs)])
    cum_b = np.concatenate([[0.0], np.cumsum(bed[order].astype(np.float64))])
    n = np.searchsorted(hs, stages, side="left")          # cells with HAND < stage
    volume = (stages * n - cum_h[n]) * cell_area
    bed_area = cum_b[n] * cell_area
    return n, volume, bed_area


def _sig(a: np.ndarray, digits: int = 4) -> list[float]:
    """compact JSON: `digits` significant digits"""
    return [float(f"{x:.{digits}g}") for x in a]


def rating_curves(hand: np.ndarray, catchments: np.ndarray, slope_deg: np.ndarray, reaches: list[dict], res: float,
                  nodata: float, manning_n: float | None = None, stages: np.ndarray | None = None) -> dict:
    """The hydrotable of one HAND model: the stage ladder and, per reach, Q / wet area / top width / hydraulic radius."""
    n_manning = settings.SRC_MANNING_N if manning_n is None else manning_n
    stages = stage_ladder() if stages is None else stages
    cell_area = res * res
    valid = (hand != nodata) & (catchments > 0)
    ids = catchments[valid]
    h_all = hand[valid].astype(np.float64)
    beta = np.where(slope_deg[valid] == nodata, 0.0, slope_deg[valid]).astype(np.float64)
    bed_all = 1.0 / np.cos(np.radians(np.clip(beta, 0.0, 89.0)))      # √(1 + tan²β)
    order = np.argsort(ids, kind="stable")
    ids_s, h_s, bed_s = ids[order], h_all[order], bed_all[order]
    uniq, start = np.unique(ids_s, return_index=True)
    bounds = dict(zip(uniq.tolist(), zip(start.tolist(), np.append(start[1:], ids_s.size).tolist())))

    rows: dict[int, dict] = {}
    for r in reaches:
        rid = int(r["id"])
        if rid not in bounds:
            continue
        a, b = bounds[rid]
        n, vol, bed = _geometry(h_s[a:b], bed_s[a:b], stages, cell_area)
        length = max(float(r["length_m"]), res)
        slope = max(float(r["slope"]), settings.SRC_SLOPE_MIN)
        wet_area = vol / length
        perim = bed / length
        with np.errstate(divide="ignore", invalid="ignore"):
            radius = np.where(perim > 0, wet_area / perim, 0.0)
        q = wet_area * np.power(radius, 2.0 / 3.0) * math.sqrt(slope) / n_manning
        q[stages == 0] = 0.0
        catch_km2 = (b - a) * cell_area / 1e6
        rows[rid] = {"id": rid, "to": int(r.get("to", 0)), "length_m": round(length, 1), "slope": round(slope, 6),
                     "slope_source": "rise_run" if float(r["slope"]) >= settings.SRC_SLOPE_MIN else "slope_min",
                     "catchment_km2": round(catch_km2, 4), "drainage_area_km2": r.get("drainage_area_km2"),
                     "short": bool(catch_km2 < settings.SRC_MIN_CATCHMENT_KM2 and length < settings.SRC_MIN_LENGTH_M),
                     "q": q, "area": wet_area, "width": n * cell_area / length, "radius": radius}

    # short reaches take the curve of the first non-short reach downstream (FIM small_segments, downstream variant)
    for row in rows.values():
        row["src_from"] = None
        if not row["short"]:
            continue
        seen, nxt = {row["id"]}, row["to"]
        while nxt in rows and rows[nxt]["short"] and nxt not in seen:
            seen.add(nxt)
            nxt = rows[nxt]["to"]
        if nxt in rows and not rows[nxt]["short"]:
            row["src_from"] = nxt

    out = []
    for row in rows.values():
        src = rows[row["src_from"]] if row["src_from"] is not None else row
        out.append({k: v for k, v in row.items() if k not in ("q", "area", "width", "radius")}
                   | {"discharge_m3s": _sig(src["q"]), "wet_area_m2": _sig(src["area"]), "top_width_m": _sig(src["width"]),
                      "hydraulic_radius_m": _sig(src["radius"])})
    return {"schema": SCHEMA, "method": "synthetic rating curve (HAND geometry + Manning, uniform flow)",
            "units": {"stage": "m above the channel (HAND = 0)", "discharge": "m3/s", "wet_area": "m2", "top_width": "m",
                      "hydraulic_radius": "m", "slope": "m/m"},
            "manning_n": n_manning, "slope_min": settings.SRC_SLOPE_MIN, "stage_step_m": float(stages[1] - stages[0]),
            "stages_m": [float(s) for s in stages], "cell_area_m2": cell_area,
            "short_reach_rule": {"catchment_km2_lt": settings.SRC_MIN_CATCHMENT_KM2, "length_m_lt": settings.SRC_MIN_LENGTH_M,
                                 "takes": "the curve of the first non-short reach downstream"},
            "references": ["Nobre et al. 2011, J. Hydrol. 404 (HAND)",
                           "Zheng, Tarboton et al. 2018, JAWRA 54(4) (HAND synthetic rating curves)",
                           "NOAA OWP inundation-mapping (FIM) v4.10, src/add_crosswalk.py, Apache-2.0"],
            "limitations": ["no channel bathymetry below the DEM water surface", "one Manning n for channel and floodplain",
                            "uniform steady flow; no backwater, structures or storage routing",
                            "reach slope from the breached DEM (rise/run), floored at slope_min"],
            "reaches": sorted(out, key=lambda r: r["id"])}


def stage_for(hydrotable_reach: dict, stages: list[float], discharge_m3s: float) -> float:
    """Q → stage on one reach's curve (linear, clamped at the ends, as FIM `inundation.py`)."""
    return float(np.interp(discharge_m3s, hydrotable_reach["discharge_m3s"], stages))
