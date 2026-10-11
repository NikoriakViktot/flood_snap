"""
Flood Control Plane — orchestrates SAR → DEM → water mask pipeline.

Runs FastAPI HTTP server + background Redis event consumer loop.
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from control_plane.infrastructure.db.session import engine, SessionLocal
from control_plane.infrastructure.db.base import Base
from control_plane.infrastructure.adapters.postgres_run_repository import PostgresRunRepository
from control_plane.infrastructure.adapters.redis_job_queue import RedisJobQueue
from control_plane.infrastructure.adapters.redis_event_bus import RedisEventBus
from control_plane.use_cases.handle_event import HandleEventUseCase
from control_plane.api import router as api_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("control_plane")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
PRODUCTS_DIR = os.getenv("PRODUCTS_DIR", "/data/products")
CORS_ORIGINS = [o for o in os.getenv("CORS_ORIGINS", "").split(",") if o]


async def _event_loop():
    """Background loop: consume cp_events, apply state machine via use case."""
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    event_bus = RedisEventBus(r)
    log.info("Event loop started, listening on 'cp_events' queue …")

    while True:
        try:
            envelope = await asyncio.to_thread(event_bus.consume, 5)
            if envelope is None:
                await asyncio.sleep(0.1)
                continue

            db = SessionLocal()
            try:
                repo = PostgresRunRepository(db)
                queue = RedisJobQueue(r)
                uc = HandleEventUseCase(repo, queue)

                uc.execute(
                    run_id=envelope["run_id"],
                    event=envelope["event"],
                    artifacts=envelope.get("artifacts", {}),
                )
            finally:
                db.close()

        except LookupError as e:
            log.warning("Event for unknown run — skipping: %s", e)
        except Exception:
            log.exception("Event loop error")
            await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    os.makedirs(PRODUCTS_DIR, exist_ok=True)
    task = asyncio.create_task(_event_loop())
    yield
    task.cancel()


app = FastAPI(
    title="Flood Control Plane",
    root_path="/api",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"status": "ok", "system": "flood_control_plane"}


if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(api_router.router)

# Basin passports (GeoJSON vectors, manifests) → {PUBLIC_BASE}/files/{basin_id}/...
os.makedirs(PRODUCTS_DIR, exist_ok=True)
app.mount("/files", StaticFiles(directory=PRODUCTS_DIR), name="files")
