from fastapi import APIRouter

from . import contracts, events, geoproc, runs

router = APIRouter()

router.include_router(contracts.router, tags=["Contracts"])
router.include_router(events.router, tags=["Events"])
router.include_router(runs.router, tags=["Runs"])
router.include_router(geoproc.router, tags=["Geoprocessing (frontend-compatible)"])
