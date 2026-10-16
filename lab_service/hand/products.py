"""The model's files as platform data products (contracts/job_event.v1.json `products[]`): every file travels in the
done event (content_b64) and the platform stores it — the Lab keeps nothing the platform depends on."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from lab_service import settings
from lab_service.pipeline import LabError

NAMES = {"hand": "HAND", "slope": "Slope", "dem_conditioning": "DEM conditioning (breached − original)",
         "stream_links": "Stream links", "catchments": "Reach catchments", "hand_png": "HAND (browser PNG)",
         "hand_aux_png": "Slope + catchments (browser PNG)", "reaches": "Reach table", "hydrotable": "Synthetic rating curves (per reach)",
         "model_config": "Model configuration",
         "metadata": "Provenance metadata"}
# what goes first when the event is over budget; the HAND COG itself is never dropped (the job fails instead)
DROP_ORDER = ("dem_conditioning", "hydrotable", "reaches", "hand_aux_png", "catchments", "stream_links", "slope", "hand_png")


def slim_hydrotable(t: dict) -> dict:
    """the hydrotable without the per-stage geometry (wet area, top width, radius): Q(stage) is what turns a discharge
    into a stage; the geometry can be recomputed from the HAND and catchment rasters"""
    keep = {"wet_area_m2", "top_width_m", "hydraulic_radius_m"}
    return {**t, "slim": True, "reaches": [{k: v for k, v in r.items() if k not in keep} for r in t["reaches"]]}


def bbox_polygon(b) -> dict:
    w, s, e, n = b
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def products_of(params: dict, run: dict, files: dict, cfg: dict, meta: dict) -> tuple[list[dict], list[str]]:
    dem = params["dem"]
    label = settings.DEM_LABELS.get(dem, {"label": dem, "version": dem, "attribution": "", "licence": ""})
    thr = f"{params['stream_threshold_km2']:g} km²"
    common = {"dem": dem, "processing_version": settings.PIPELINE_VERSION,
              "source_attribution": f"GeoHydroAI Flood Lab HAND model {settings.PIPELINE_VERSION} — {label['version']}; {label['attribution']}",
              "license": f"derived from {label['label']}: {label['licence']}"}
    items: list[dict] = []
    for kind, f in files.items():
        data = Path(f["path"]).read_bytes()
        items.append({**common, "kind": kind, "name": f"{NAMES[kind]} · {label['label']} · {thr}", "file_name": f["file_name"],
                      "media_type": f["media_type"], "crs": f["crs"], "resolution_m": f["resolution_m"],
                      "bbox": bbox_polygon(f["bounds"]),
                      "stats": {**f["stats"], "hand_model": params.get("hand_model_id"), "bytes": len(data)}, "_bytes": data})
    for kind, obj, fname in (("reaches", run["reaches"], "reaches.json"), ("hydrotable", run["hydrotable"], "hydrotable.json"),
                             ("model_config", cfg, "model_config.json"),
                             ("metadata", meta, "metadata.json")):
        # the hydrotable is large (stages × reaches): compact JSON, and only Q(stage) when the geometry does not fit
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) if kind == "hydrotable"
                else json.dumps(obj, ensure_ascii=False, indent=1)).encode()
        if kind == "hydrotable" and len(data) > settings.HAND_PRODUCT_MAX_MB * 2 ** 20:
            obj = slim_hydrotable(obj)
            data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        items.append({**common, "kind": kind, "name": f"{NAMES[kind]} · {label['label']} · {thr}", "file_name": fname,
                      "media_type": "application/json", "crs": "EPSG:3035", "resolution_m": None,
                      "bbox": bbox_polygon(run["bounds_4326"]),
                      "stats": {"hand_model": params.get("hand_model_id"), "bytes": len(data)}, "_bytes": data})
    limit = settings.HAND_PRODUCT_MAX_MB * 2 ** 20
    dropped: list[str] = []
    for it in list(items):
        if len(it["_bytes"]) > limit:
            if it["kind"] == "hand":
                raise LabError(f"the HAND COG is {len(it['_bytes']) / 2 ** 20:.1f} MB, above the {settings.HAND_PRODUCT_MAX_MB} MB "
                               "inline limit: the basin is too large for this delivery")
            items.remove(it)
            dropped.append(it["kind"])
    total = sum(len(it["_bytes"]) for it in items)
    for kind in DROP_ORDER:
        if total <= settings.HAND_EVENT_MAX_MB * 2 ** 20:
            break
        it = next((x for x in items if x["kind"] == kind), None)
        if it is not None:
            items.remove(it)
            dropped.append(kind)
            total -= len(it["_bytes"])
    if total > settings.HAND_EVENT_MAX_MB * 2 ** 20:
        raise LabError(f"the model's products ({total / 2 ** 20:.0f} MB) exceed the {settings.HAND_EVENT_MAX_MB} MB event budget")
    if dropped:
        run["stats"]["quality"]["warnings"].append(f"products left out of the event (size budget): {', '.join(dropped)}")
    out = []
    for it in items:
        data = it.pop("_bytes")
        it["content_b64"] = base64.b64encode(data).decode("ascii")
        out.append(it)
    return out, dropped
