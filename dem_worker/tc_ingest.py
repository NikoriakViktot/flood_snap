"""
Terracotta ingest: register a basin's COGs in the Terracotta SQLite database.

Runs as a separate step after the worker has written manifest.json. Keys are
(category, layer); every layer embeds the basin id so basins never overwrite
each other. The Terracotta server (compose service `terracotta`) reads the same
SQLite file from the shared /data volume.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("tc_ingest")

TC_DB = Path(os.environ.get("TC_DB", "/data/tc/terracotta.sqlite"))
TC_KEYS = ("category", "layer")


def ingest_manifest(manifest: dict, products_dir: Path) -> int:
    """Insert every raster product of a manifest. Returns the number of datasets written."""
    from terracotta import get_driver, update_settings

    update_settings(RASTER_CACHE_SIZE=0)  # metadata only; the server does the tiling
    TC_DB.parent.mkdir(parents=True, exist_ok=True)
    driver = get_driver(str(TC_DB), provider="sqlite")
    if not TC_DB.exists():
        driver.create(TC_KEYS)
        log.info("created Terracotta DB %s with keys %s", TC_DB, TC_KEYS)

    n = 0
    with driver.connect():
        for p in manifest["products"]:
            for item in p.get("datasets", [{"tc_layer": p["tc_layer"], "cog": p["cog"]}]):
                path = products_dir / item["cog"]
                keys = {"category": p["tc_category"], "layer": item["tc_layer"]}
                if not path.exists():
                    log.warning("skip %s: %s missing", keys, path)
                    continue
                driver.insert(keys, str(path))
                n += 1
    log.info("Terracotta: %d datasets ingested for basin %s", n, manifest["basin_id"])
    return n
