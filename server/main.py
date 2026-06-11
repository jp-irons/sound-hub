"""Sound Hub — FastAPI backend.

Responsibilities (Phase 1/2 of the basestation plan):
  - Maintain a registry of known SCN nodes (SQLite-backed identity, in-memory live status)
  - Discover nodes automatically via mDNS (discovery.py)
  - Poll known nodes for live status (poller.py)
  - Expose a REST API for the React frontend (routes.py)

Run with:  uvicorn server.main:app --reload --port 8000
The Vite dev server (default :5173) is allowed via CORS below.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import birdnet_worker, db, discovery, poller
from .routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("sound_hub.main")

_poller_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poller_task
    await db.init_db()
    await discovery.start()
    _poller_task = asyncio.create_task(poller.run())
    # Load BirdNET model in a thread so the event loop is not blocked.
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, birdnet_worker.init)
    log.info("Sound Hub backend ready")
    yield
    log.info("Shutting down...")
    if _poller_task is not None:
        _poller_task.cancel()
    await discovery.stop()


app = FastAPI(title="Sound Hub", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")
