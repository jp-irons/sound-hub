"""Sound Hub — FastAPI backend.

Responsibilities (Phase 1/2 of the basestation plan):
  - Maintain a registry of known SCN nodes (SQLite-backed identity, in-memory live status)
  - Nodes find the hub via self-registration on boot (routes.py: /api/nodes/register) or a
    manual add (/api/nodes/manual) — mDNS discovery was removed 2026-07-12 (see project
    memory `project-mdns-to-dns-migration`); DNS (.irons.net.au) is now the sole path.
  - Poll known nodes for live status (poller.py)
  - Expose a REST API for the React frontend (routes.py)

Run with:  uvicorn server.main:app --reload --port 8000
The Vite dev server (default :5173) is allowed via CORS below.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from . import birdnet_worker, db, poller, routes
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
    if await db.count_users() == 0:
        log.warning(
            "⚠  No user accounts configured. "
            "POST to http://%s:%s/api/auth/setup to create the admin account.",
            "localhost", 8000,
        )
    await routes.init_relay_client()
    _poller_task = asyncio.create_task(poller.run())
    # Load BirdNET model in a thread so the event loop is not blocked.
    await asyncio.get_running_loop().run_in_executor(None, birdnet_worker.init)
    log.info("BirdNET model loaded")
    yield
    if _poller_task:
        _poller_task.cancel()
    await routes.close_relay_client()


app = FastAPI(title="Sound Hub", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:4173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")
