"""The child process of a hand_model job (spawned by worker.execute with target=hand_child, address-space limited)."""
from __future__ import annotations

import json
import logging
import resource
import traceback
from datetime import datetime, timezone
from pathlib import Path

from lab_service import settings

log = logging.getLogger("lab")


def hand_child(job: dict, out_dir: str, conn) -> None:
    try:
        resource.setrlimit(resource.RLIMIT_AS, (settings.CHILD_MEM_BYTES, settings.CHILD_MEM_BYTES))
    except (ValueError, OSError):
        pass
    import whitebox

    from lab_service import pipeline as P
    from lab_service.hand import packaging as PK
    from lab_service.hand import pipeline as HP
    from lab_service.hand import products as PR
    from lab_service.hand import provenance as PV
    from lab_service.terrain import TerrainStore
    out = Path(out_dir)
    try:
        p = job["params"]
        terrain = TerrainStore()
        wbt = whitebox.WhiteboxTools()
        wbt.set_verbose_mode(False)

        def progress(stage: str) -> None:
            conn.send(("progress", HP.STAGES.get(stage, 50), stage))

        progress("terrain")
        paths = terrain.paths(p["dem"], p["district_slug"], ("d8", "streams", "dem_f32"))
        run = HP.run_hand_model(wbt, p["dem"], paths, p, out / "_tmp", progress)
        progress("packaging")
        files = PK.package(run, out)
        ver = PV.versions(wbt)
        cfg = PV.model_config(job["job_id"], p, run, terrain.entry(p["dem"], p["district_slug"]), terrain.manifest(p["dem"]), ver)
        computed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta = PV.metadata(job["job_id"], p, run, cfg, files, ver, computed_at)
        for name, obj in (("model_config.json", cfg), ("metadata.json", meta), ("reaches.json", run["reaches"])):
            (out / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1))
        products, dropped = PR.products_of(p, run, files, cfg, meta)
        result = {"kind": "hand_model", "stats": run["stats"], "metadata": meta, "config_hash": cfg["config_hash"],
                  "snapped_outlet": run["snapped_outlet"], "dropped_products": dropped}
        (out / "result.json").write_text(json.dumps(result, ensure_ascii=False))
        P.cleanup(out / "_tmp")
        conn.send(("done", {"result": result, "products": products}))
    except P.LabError as e:
        conn.send(("failed", str(e)))
    except MemoryError:
        conn.send(("failed", "out of memory: the basin is too large for a HAND model on this worker"))
    except Exception as e:  # noqa: BLE001
        log.error("job %s crashed:\n%s", job["job_id"], traceback.format_exc())
        conn.send(("failed", f"{type(e).__name__}: {e}"))
    finally:
        conn.close()
