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

from datetime import datetime, timezone

from fastapi import FastAPI, Request
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
    if await db.count_users() == 0:
        log.warning(
            "⚠  No user accounts configured. "
            "POST to http://%s:%s/api/auth/setup to create the admin account.",
            "localhost", 8000,
        )
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


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    """Log all authenticated admin actions to the audit_log table."""
    response = await call_next(request)
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        username = getattr(request.state, "auth_user", None)
        if username:
            await db.write_audit_entry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                username=username,
                source_ip=request.client.host if request.client else None,
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
            )
    return response


app.include_router(router, prefix="/api")
