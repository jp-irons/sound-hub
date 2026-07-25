"""API routes consumed by the React frontend (mounted under /api in main.py)."""
import asyncio
import functools
import json
import logging
import math
import os
import tempfile
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from . import birdnet_worker, config, correlation, db, onset_detection, registry, species_links, status_mapper, suntimes
from .auth import get_current_user, require_admin, require_node, require_viewer
from .models import (
    AckStatus,
    ArrayOrigin, ArrayOriginManual, AudioAnalytics, AudioCleanupSettings,
    AudioCleanupSettingsUpdate, AudioEventRecord,
    AudioAckBody, AudioSampleRequest, DetectionRecord, ManualNodeRequest,
    NodeAudioSummary, NodeConfigRequest, NodeHeartbeatRequest, NodePosition,
    PositionFromEma,
    NodeRegisterRequest,
    NodeTriggerSummary, NodeView, SpeciesLinksResolveResult, SpeciesSummary,
    SpeciesTdoaParams,
    SpeciesTdoaParamsRecord, TdoaAttemptNodeRecord, TdoaAttemptRecord,
    TdoaRequest, TdoaResponse,
    LatLon, TriggerDiagAnalytics, TriggerEventRecord,
    RatioHistogram, RatioHistogramBucket, TriggerHistogramResponse,
    TriggerRollupBucket, TriggerRollupResponse,
)
from .tdoa_solver import (
    DEFAULT_SPEED_OF_SOUND,
    WORST_CASE_SPEED_OF_SOUND,
    Node as TdoaNode,
    solve as tdoa_solve,
)

# Directory for saved audio files (created on first push).
_AUDIO_DIR = os.path.join(os.path.dirname(__file__), "..", "audio")
# Public alias — audio_cleanup.py prunes this directory on a retention timer
# and needs the path without reaching into a private module attribute.
AUDIO_DIR = _AUDIO_DIR

log = logging.getLogger("sound_hub.routes")
router = APIRouter()

# Shared client for node/broker relay calls (audio pulls — see
# _issue_sample_pull). Reused across calls instead of opening a fresh
# httpx.AsyncClient (and paying a fresh TLS handshake) per pull — same
# rationale as poller.py's single long-lived client. TDOA orchestration can
# fan out to several neighbours per detection, so this matters more here
# than it looks. Initialised/closed from the FastAPI lifespan (main.py).
_relay_client: httpx.AsyncClient | None = None


async def init_relay_client() -> None:
    global _relay_client
    _relay_client = httpx.AsyncClient(verify=False, timeout=5.0)


async def close_relay_client() -> None:
    global _relay_client
    if _relay_client is not None:
        await _relay_client.aclose()
        _relay_client = None


# ---------------------------------------------------------------------------
# Auth request / response models
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str

class SetupRequest(BaseModel):
    username: str
    password: str

class UserInfo(BaseModel):
    username: str
    role: str

class UserListItem(BaseModel):
    username: str
    role: str
    created_at: str

class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "viewer"

class ChangePasswordRequest(BaseModel):
    password: str


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@router.post("/auth/setup", response_model=UserInfo, status_code=201)
async def setup_first_user(req: SetupRequest):
    """Create the initial admin account.

    Only available while the users table is empty — returns 404 once any
    user exists.  Hit this endpoint after first startup to initialise credentials.
    """
    if await db.count_users() > 0:
        raise HTTPException(status_code=404, detail="Not found")
    if not req.username or not req.password:
        raise HTTPException(status_code=422, detail="Username and password required")
    from .auth import hash_password
    hashed = hash_password(req.password)
    await db.create_user(req.username, hashed, "admin", _now_iso())
    log.info("First admin account created: %s", req.username)
    return UserInfo(username=req.username, role="admin")


@router.post("/auth/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    """Validate credentials and return a JWT Bearer token."""
    from .auth import create_token, verify_password
    user = await db.get_user(req.username)
    if user is None or not verify_password(req.password, user["hashed_password"]):
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_token(user["username"], user["role"])
    log.info("Login: %s", user["username"])
    return LoginResponse(access_token=token, role=user["role"])


@router.get("/auth/me", response_model=UserInfo)
async def me(user: dict = Depends(get_current_user)):
    """Return the current authenticated user's identity."""
    return UserInfo(username=user["username"], role=user["role"])


@router.get("/auth/status")
async def auth_status():
    """Return whether first-run setup is still required.

    Called by the SPA on load to decide whether to show the setup screen
    or the login screen.  No auth required.
    """
    return {"setup_required": await db.count_users() == 0}


# ---------------------------------------------------------------------------
# User management  (admin only)
# ---------------------------------------------------------------------------

@router.get("/users", response_model=list[UserListItem], dependencies=[Depends(require_admin)])
async def list_users():
    """List all active user accounts."""
    return await db.list_users()


@router.post("/users", response_model=UserListItem, status_code=201, dependencies=[Depends(require_admin)])
async def create_user(req: CreateUserRequest):
    """Create a new user account."""
    if not req.username.strip():
        raise HTTPException(status_code=422, detail="Username is required")
    if len(req.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    if req.role not in ("admin", "viewer"):
        raise HTTPException(status_code=422, detail="Role must be 'admin' or 'viewer'")
    if await db.get_user(req.username.strip()) is not None:
        raise HTTPException(status_code=409, detail="Username already exists")

    from .auth import hash_password
    hashed = hash_password(req.password)
    await db.create_user(req.username.strip(), hashed, req.role, _now_iso())
    log.info("User created: %s (role=%s)", req.username.strip(), req.role)
    return UserListItem(username=req.username.strip(), role=req.role, created_at=_now_iso())


@router.delete("/users/{username}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_user(username: str, caller: dict = Depends(require_admin)):
    """Delete a user account.

    Raises 409 if the target is the last active admin (lockout prevention).
    Raises 404 if the user does not exist.
    """
    target = await db.get_user(username)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target["role"] == "admin" and await db.count_active_admins() <= 1:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete the last admin account",
        )
    await db.delete_user(username)
    log.info("User deleted: %s", username)


@router.put("/users/{username}/password", status_code=204, dependencies=[Depends(require_admin)])
async def change_password(username: str, req: ChangePasswordRequest):
    """Change a user's password."""
    if len(req.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    from .auth import hash_password
    updated = await db.update_user_password(username, hash_password(req.password))
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    log.info("Password changed for: %s", username)


# ---------------------------------------------------------------------------
# In-memory audio request tracker.
# Keyed by requestId (int).  Each entry: {"acks": [{"status", "srcMac", "at"}, ...]}
# Reset on process restart — this is intentional for Phase 1 (test/debug aid).
_audio_requests: dict[int, dict] = {}

# ---------------------------------------------------------------------------
# TDOA detection coalescing (see project_soundhub_tdoa_dedup design notes).
#
# Multiple nodes hearing the same call independently push detections within
# milliseconds of each other. Without coalescing, every one of those pushes
# fires its own _plan_tdoa_attempt, each pulling from every *other* node —
# an N-node array with one shared event turns into up to N*(N-1) pull
# requests for what is fundamentally the same acoustic event.
#
# Detections are buffered per-species into short-lived "clusters" keyed on
# species_key, anchored to the first detection's arrival (the debounce is
# NOT reset by later joiners — a steady trickle of detections must not be
# able to postpone planning indefinitely). When the debounce elapses, the
# highest-confidence member becomes the TDOA attempt's origin and every
# other member is recorded as an already-known corroborator rather than a
# fresh pull target.
TDOA_COALESCE_DEBOUNCE_MS = 100

# How close two detections' capture windows must be (in addition to sharing
# a species_key) to be treated as the same underlying event. Deliberately a
# fixed constant rather than a per-species travel-time-floor computation —
# clustering happens synchronously on the request path and shouldn't need an
# extra DB round trip; the real precision (window padding) already happens
# later in _plan_tdoa_attempt_inner once travel_time_floor is known.
TDOA_CLUSTER_OVERLAP_TOLERANCE_MS = 250

# species_key -> list of open clusters. Each cluster:
#   {"members": [{"node_id", "audio_event_id", "confidence",
#                 "t_start_us", "t_end_us"}, ...], "task": asyncio.Task}
# A species can have more than one concurrently-open cluster (e.g. two
# separate calls of the same species a second apart) — clusters are
# distinguished by window overlap, not just species_key.
_pending_clusters: dict[str, list[dict]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _node_position_from_row(pos: dict) -> NodePosition:
    """Build a NodePosition from a node_positions DB row."""
    return NodePosition(
        pos_e=pos["pos_e"],
        pos_n=pos["pos_n"],
        pos_alt=pos["pos_alt"],
        pos_status=pos["pos_status"],
        is_origin=bool(pos["is_origin"]),
        origin_lat=pos["origin_lat"],
        origin_lon=pos["origin_lon"],
        origin_alt=pos["origin_alt"],
    )


def _build_view(node: dict, live: dict, derived: dict) -> NodeView:
    return NodeView(
        id=node["id"],
        hostname=node["hostname"],
        ip_address=node["ip_address"],
        role=derived["role"],
        discovery_method=node["discovery_method"],
        approval_status=node["approval_status"],
        configured=bool(node["configured"]),
        reachable=live["reachable"],
        last_seen_at=live["last_seen_at"],
        raw_status=live["raw_status"],
        status=derived["status"],
        lat_lon=derived["lat_lon"],
        position_relative=derived["position_relative"],
        position_known=derived["position_known"],
        position_status=derived["position_status"],
        is_origin=derived["is_origin"],
        survey_disagreement_m=derived["survey_disagreement_m"],
        gps=derived["gps"],
        clock=derived["clock"],
        audio=derived["audio"],
        esp_now=derived["esp_now"],
        firmware_version=derived.get("firmware_version"),
        flags=derived["flags"],
        reg_heap_free_bytes=live.get("reg_heap_free_bytes"),
        reg_heap_min_free_bytes=live.get("reg_heap_min_free_bytes"),
        reg_heap_at=live.get("reg_heap_at"),
        reg_https_active_sockets=live.get("reg_https_active_sockets"),
        reg_https_max_sockets=live.get("reg_https_max_sockets"),
    )


async def _mapped_nodes() -> list[tuple[dict, dict, dict]]:
    """Map every registered node's raw status, incorporating hub positions.

    Loads hub position records and the array_origin in one pass, then maps
    each node.  Cross-node derivation:
      - derive_relative_positions: projects all nodes with stored E/N offsets
        onto the hub array_origin geographic datum.
    """
    nodes = await registry.list_nodes()
    positions = await db.list_node_positions()
    array_origin = await db.get_array_origin()   # None until operator sets it

    triples = []
    for node in nodes:
        live = registry.get_live_status(node["id"])
        node_pos = positions.get(node["id"])
        derived = status_mapper.map_status(
            role=node["role"],
            reachable=live["reachable"],
            raw_status=live["raw_status"],
            node_pos=node_pos,
            array_origin=array_origin,
            gps_ema=registry.get_gps_ema(node["id"]),
        )
        triples.append((node, live, derived))

    mapped = [derived for _, _, derived in triples]
    status_mapper.derive_relative_positions(mapped, array_origin=array_origin)
    return triples


# ---------------------------------------------------------------------------
# Public endpoints — no auth required, safe for external exposure.
# Returns a slim subset of node data: position + status only.
# IP addresses, firmware versions, raw status, and all diagnostic
# internals are deliberately excluded.
# ---------------------------------------------------------------------------

class PublicNodeView(BaseModel):
    """Slim node view for unauthenticated / public map access."""
    model_config = ConfigDict(populate_by_name=True)

    id: str
    hostname: str
    role: str
    status: str
    lat_lon: LatLon | None = Field(default=None, alias="latLon")
    position_known: bool   = Field(default=False, alias="positionKnown")
    is_origin: bool        = Field(default=False, alias="isOrigin")


@router.get("/public/nodes", response_model=list[PublicNodeView])
async def public_list_nodes():
    """Unauthenticated node list for public map display.

    Only returns approved nodes.  Omits all sensitive fields (IP address,
    firmware version, raw status, clock/audio/GPS internals).
    """
    return [
        PublicNodeView(
            id=node["id"],
            hostname=node["hostname"],
            role=derived["role"],
            status=derived["status"],
            lat_lon=derived["lat_lon"],
            position_known=derived["position_known"],
            is_origin=derived["is_origin"],
        )
        for node, _live, derived in await _mapped_nodes()
        if node["approval_status"] == db.APPROVED
    ]


@router.get("/public/origin")
async def public_origin():
    """Unauthenticated array origin for map centering.

    Returns only lat/lon/alt — no audit metadata.
    """
    origin = await db.get_array_origin()
    if origin is None:
        raise HTTPException(status_code=404, detail="Array origin not configured")
    return {"lat": origin["lat"], "lon": origin["lon"], "altM": origin["alt_m"]}


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/nodes", response_model=list[NodeView], dependencies=[Depends(require_viewer)])
async def list_nodes():
    return [_build_view(node, live, derived) for node, live, derived in await _mapped_nodes()]


@router.get("/nodes/{node_id}", response_model=NodeView, dependencies=[Depends(require_viewer)])
async def get_node(node_id: str):
    for node, live, derived in await _mapped_nodes():
        if node["id"] == node_id:
            return _build_view(node, live, derived)
    raise HTTPException(status_code=404, detail="Node not found")


@router.post("/nodes/manual", response_model=NodeView, dependencies=[Depends(require_admin)])
async def add_manual_node(req: ManualNodeRequest):
    """Add a node by hostname or bare IP — the fallback (and, once
    self-registration is eventually retired, the only) discovery path.

    Reachability is best-effort, not required. If the node responds to its
    own status API, its self-reported hostname becomes the canonical node
    id and live status is seeded immediately — unchanged from before. If it
    doesn't respond, the node is still added — keyed by whatever string was
    typed here — so an operator can pre-provision a node that isn't
    deployed/powered on yet. It picks up real status automatically the next
    time the poller (or a self-registration, while that still exists)
    successfully reaches it.

    Trade-off vs the old hard-reachability requirement: a typo'd
    hostname/IP no longer errors immediately — it just sits as a quiet,
    permanently-unreachable pending node until noticed and removed (see
    DELETE /nodes/{node_id} below). And identity only converges with a
    later self-registration if the *hostname* (not the IP) is what's typed
    here — self-registration keys nodes by their self-reported hostname, so
    typing an IP creates a separate, stale row once the real node registers
    under its own name.
    """
    host = req.host.strip()
    if not host:
        raise HTTPException(status_code=400, detail="host must not be empty")

    node_id = host
    status_json = None
    reachable = False

    url = f"{config.NODE_SCHEME}://{host}/app/api/status"
    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(url, timeout=config.STATUS_TIMEOUT_S)
            resp.raise_for_status()
            status_json = resp.json()
        node_id = (status_json.get("node") or {}).get("hostname") or host
        reachable = True
    except (httpx.HTTPError, ValueError):
        pass  # not reachable right now — still add it, see docstring above

    await registry.upsert_node(node_id=node_id, hostname=node_id, ip_address=host,
                               discovery_method="manual")
    if reachable:
        registry.update_live_status(node_id, reachable=True, raw_status=status_json)
        # Push the hub's own address so the node doesn't need hubAddress set
        # by hand via its own web UI. Best-effort — a failure here just
        # leaves `configured` unset and poller._poll_one retries it on the
        # node's next successful poll.
        if await push_hub_address_to_node(host):
            await registry.set_configured(node_id, True)

    for n, live, derived in await _mapped_nodes():
        if n["id"] == node_id:
            return _build_view(n, live, derived)
    raise HTTPException(status_code=500, detail="Node registered but not found in mapped set")


@router.post("/nodes/register", response_model=NodeView, dependencies=[Depends(require_node)])
async def register_node(req: NodeRegisterRequest, request: Request):
    """Node self-registration on boot.  No JWT required — LAN subnet check only.

    The node supplies its hostname and MAC; the hub derives the IP from the TCP
    connection so the node never has to know its own address.  New nodes land
    with approval_status='pending'; re-registration of an already-approved node
    leaves its approval status untouched.
    """
    node_id = req.hostname
    ip = request.client.host
    await registry.upsert_node(
        node_id=node_id, hostname=node_id,
        ip_address=ip, discovery_method="self_registered",
    )
    registry.update_registration_heap(
        node_id, req.heap_free_bytes, req.heap_min_free_bytes,
    )
    registry.update_registration_sockets(
        node_id, req.https_active_sockets, req.https_max_sockets,
    )

    async def _background_status():
        try:
            url = f"{config.NODE_SCHEME}://{ip}/app/api/status"
            async with httpx.AsyncClient(verify=False) as client:
                resp = await client.get(url, timeout=config.STATUS_TIMEOUT_S)
                resp.raise_for_status()
                registry.update_live_status(node_id, reachable=True, raw_status=resp.json())
        except Exception:
            # Node called us, so it's reachable — but status fetch failed.
            registry.update_live_status(node_id, reachable=True, raw_status=None)

    asyncio.create_task(_background_status())

    for n, live, derived in await _mapped_nodes():
        if n["id"] == node_id:
            return _build_view(n, live, derived)
    raise HTTPException(status_code=500, detail="Node registered but not found")


@router.post("/nodes/{node_id}/heartbeat", dependencies=[Depends(require_node)])
async def node_heartbeat(node_id: str, req: NodeHeartbeatRequest, request: Request):
    """Periodic plain-HTTP telemetry ping from an already-known node (see
    firmware's HubHeartbeat, formerly HubRegistrar's registerOnce/register).

    Not a discovery/self-registration path, unlike register_node above —
    404s if node_id isn't already a known node. Identity/discovery is
    entirely the hub's job now (see add_manual_node / the 2026-07-12
    registration-architecture redesign); a node calling this before an
    operator has added it has nothing to attach telemetry to.

    Still records reachable=True unconditionally on arrival, independent of
    the HTTPS poller — preserves the one behavior register_node() had that
    the poller alone can't provide: a node can be plain-HTTP-reachable
    while its HTTPS status server is specifically degraded (see the .150
    TLS-reset investigation), and this heartbeat is that node's only
    channel to still show as reachable during exactly that failure mode.

    Also refreshes ip_address from the request's source IP (see
    registry.update_node_ip) — a lightweight defence against a node's DHCP
    lease changing without an operator re-adding it.
    """
    node = await registry.get_node(node_id)
    if node is None:
        raise HTTPException(
            status_code=404,
            detail="Unknown node — add it via POST /api/nodes/manual first",
        )

    registry.update_live_status(node_id, reachable=True, raw_status=None)
    registry.update_registration_heap(
        node_id, req.heap_free_bytes, req.heap_min_free_bytes,
    )
    registry.update_registration_sockets(
        node_id, req.https_active_sockets, req.https_max_sockets,
    )
    await registry.update_node_ip(node_id, request.client.host)

    return {"ok": True}


@router.delete("/nodes/{node_id}", status_code=204, dependencies=[Depends(require_admin)])
async def remove_node(node_id: str):
    node = await registry.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    await registry.remove_node(node_id)


async def _set_approval(node_id: str, status: str) -> NodeView:
    node = await registry.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    await registry.set_approval_status(node_id, status)
    for n, live, derived in await _mapped_nodes():
        if n["id"] == node_id:
            return _build_view(n, live, derived)
    raise HTTPException(status_code=500, detail="Node updated but not found in mapped set")


@router.post("/nodes/{node_id}/approve", response_model=NodeView, dependencies=[Depends(require_admin)])
async def approve_node(node_id: str):
    """Admit a discovered node into the active array."""
    return await _set_approval(node_id, db.APPROVED)


@router.post("/nodes/{node_id}/reject", response_model=NodeView, dependencies=[Depends(require_admin)])
async def reject_node(node_id: str):
    """Decline a discovered node — keep it out of the active array."""
    return await _set_approval(node_id, db.REJECTED)


@router.get("/nodes/{node_id}/config", dependencies=[Depends(require_admin)])
async def get_node_config(node_id: str):
    """Fetch a node's current persisted config — used to pre-fill the
    operator's edit form so they're editing real values, not guessing."""
    node = await registry.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")

    url = f"{config.NODE_SCHEME}://{node['ip_address']}/app/api/node-config"
    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(url, timeout=config.STATUS_TIMEOUT_S)
            resp.raise_for_status()
            return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not read config from node: {exc}",
        )


@router.post("/nodes/{node_id}/configure", dependencies=[Depends(require_admin)])
async def configure_node(node_id: str, req: NodeConfigRequest):
    """Push config changes to a node by proxying to its own
    POST /app/api/node-config (NodeConfigHandler.cpp — persists to NVS).

    Post Track A: only isBroker (and future audio params) are node-resident
    config. Position fields are hub-managed; use PUT /nodes/{id}/position.
    """
    node = await registry.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")

    body = req.model_dump(by_alias=True, exclude_unset=True, exclude_none=True)

    url = f"{config.NODE_SCHEME}://{node['ip_address']}/app/api/node-config"
    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.post(url, json=body, timeout=config.STATUS_TIMEOUT_S)
            resp.raise_for_status()
            return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not push config to node: {exc}",
        )


async def push_hub_address_to_node(host: str, client: httpx.AsyncClient | None = None) -> bool:
    """POST the hub's own address to a node's /app/api/node-config, so the
    node doesn't need hubAddress configured by hand via its own web UI.

    Called from add_manual_node (right after a reachable add) and from
    poller._poll_one (the first successful poll of a not-yet-configured
    node — covers the pre-provisioned/unreachable-at-add-time case). Both
    callers gate on registry's `configured` flag and only call this once
    per node, so this deliberately does not get re-sent on every poll.

    Best-effort, like the rest of this file's node-facing calls — returns
    False rather than raising, so a failure just leaves `configured` unset
    and the next successful reach retries it.

    client lets poller.py pass its own long-lived AsyncClient instead of
    paying for a fresh connection every 5s poll tick; omitted, a short-lived
    one is opened here instead (matching add_manual_node/configure_node).
    """
    url = f"{config.NODE_SCHEME}://{host}/app/api/node-config"
    body = {"hubAddress": config.BASE_STATION_IP}
    try:
        if client is not None:
            resp = await client.post(url, json=body, timeout=config.STATUS_TIMEOUT_S)
        else:
            async with httpx.AsyncClient(verify=False) as one_off:
                resp = await one_off.post(url, json=body, timeout=config.STATUS_TIMEOUT_S)
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("push_hub_address_to_node: failed for %s: %s", host, exc)
        return False


@router.post("/nodes/repush-hub-address", dependencies=[Depends(require_admin)])
async def repush_hub_address():
    """Re-push the hub's address to every approved node with a known IP,
    bypassing the `configured` gate that normally makes
    push_hub_address_to_node a one-time thing per node (see add_manual_node
    and poller._poll_one).

    That gate is deliberate day-to-day — nodes shouldn't be re-POSTed every
    5s poll tick — but it means a node that's already `configured` never
    notices if BASE_STATION_IP itself changes (e.g. the hub moving to new
    hardware). This is the manual trigger for that one-off event; wired to
    the "Re-push hub address to all nodes" button in the Settings tab.
    """
    nodes = [n for n in await registry.list_nodes()
             if n["approval_status"] == db.APPROVED and n["ip_address"]]

    results = await asyncio.gather(
        *(push_hub_address_to_node(n["ip_address"]) for n in nodes)
    )

    pushed: list[str] = []
    failed: list[str] = []
    for n, ok in zip(nodes, results):
        if ok:
            await registry.set_configured(n["id"], True)
            pushed.append(n["id"])
        else:
            failed.append(n["id"])

    return {"pushed": pushed, "failed": failed}


# ---------------------------------------------------------------------------
# Node position management (hub-owned, not proxied to node)
# ---------------------------------------------------------------------------

@router.get("/nodes/{node_id}/position", response_model=NodePosition, dependencies=[Depends(require_viewer)])
async def get_node_position(node_id: str):
    """Return the hub-stored position for a node.

    404 if the node doesn't exist; 204-equivalent (empty body) represented
    as null fields if no position has been set yet.
    """
    node = await registry.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    pos = await db.get_node_position(node_id)
    if pos is None:
        # No position set yet — return an empty/default record rather than 404
        # (404 would be ambiguous: "node not found" vs "position not set").
        return NodePosition()
    return _node_position_from_row(pos)


@router.put("/nodes/{node_id}/position", response_model=NodePosition, dependencies=[Depends(require_admin)])
async def set_node_position(node_id: str, req: NodePosition):
    """Set or update the hub-stored position for a node."""
    node = await registry.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")

    # is_origin on node_positions is a legacy field — nothing sets it true any
    # more (hub origin provenance lives entirely in array_origin.set_from).
    # Still accepted here for backward compat with old request bodies, but
    # has no effect on hub origin behavior.
    await db.upsert_node_position(
        node_id=node_id,
        pos_e=req.pos_e,
        pos_n=req.pos_n,
        pos_alt=req.pos_alt,
        pos_status=req.pos_status,
        is_origin=req.is_origin,
        origin_lat=req.origin_lat,
        origin_lon=req.origin_lon,
        origin_alt=req.origin_alt,
        updated_at=_now_iso(),
    )

    pos = await db.get_node_position(node_id)
    return _node_position_from_row(pos)


@router.get("/nodes/{node_id}/position/from-ema", response_model=PositionFromEma,
            dependencies=[Depends(require_viewer)])
async def get_position_from_ema(node_id: str):
    """Preview this node's E/N/Alt offset as computed from its current
    hub-side GPS EMA, back-projected through the array origin.

    Read-only — nothing is persisted. The operator reviews the preview and,
    if happy with it, applies it via PUT /nodes/{node_id}/position.
    """
    node = await registry.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")

    origin = await db.get_array_origin()
    if origin is None:
        raise HTTPException(status_code=422, detail="Array origin not configured")

    ema = registry.get_gps_ema(node_id)
    if ema is None:
        raise HTTPException(
            status_code=422,
            detail="No GPS EMA yet for this node — wait for GPS to lock and settle",
        )

    # Forward-project: this node's offset from the origin, given both the
    # origin and this node's current absolute position (the EMA). Same
    # underlying math as the origin-from-node back-projection, solving for
    # the other unknown.
    pos_n = (ema["lat"] - origin["lat"]) * status_mapper._M_PER_DEG_LAT
    pos_e = (ema["lon"] - origin["lon"]) * status_mapper._M_PER_DEG_LON
    pos_alt = ema["alt"] - origin["alt_m"]

    return PositionFromEma(
        pos_e=pos_e, pos_n=pos_n, pos_alt=pos_alt,
        ema_lat=ema["lat"], ema_lon=ema["lon"], ema_alt=ema["alt"], ema_n=ema["n"],
    )


# ---------------------------------------------------------------------------
# Hub array origin  (geographic datum — independent of any node)
# ---------------------------------------------------------------------------

@router.get("/origin", response_model=ArrayOrigin, dependencies=[Depends(require_viewer)])
async def get_origin():
    """Return the current hub array origin, or 404 if not yet configured."""
    origin = await db.get_array_origin()
    if origin is None:
        raise HTTPException(status_code=404, detail="Array origin not configured")
    return ArrayOrigin(
        lat=origin["lat"],
        lon=origin["lon"],
        alt_m=origin["alt_m"],
        set_from=origin.get("set_from"),
        set_at=origin["set_at"],
    )


@router.post("/origin/set-from-node/{node_id}", response_model=ArrayOrigin, dependencies=[Depends(require_admin)])
async def set_origin_from_node(node_id: str):
    """Compute and store the hub array origin from a surveyed node's position.

    Back-projects the array (0,0,0) datum from the node's live GPS EMA
    (registry.get_gps_ema — an in-memory hub-side average, see registry.py)
    minus its stored N/E/Alt array offset:
        origin = ema_latlon - node_array_offset

    The node's N/E/Alt offset is preserved so all other surveyed nodes
    remain valid with no re-surveying required.

    A node used this way is not given any ongoing "reference node" status —
    the hub origin is a standalone setting (see PUT /origin for setting it
    directly, with no node involved at all — the route to use if you have an
    independent absolute reference for the origin itself, e.g. from a
    survey-grade GNSS unit or a known landmark). Any stale is_origin marker
    from a previous call is cleared rather than reassigned.
    """
    node = await registry.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")

    positions = await db.list_node_positions()
    node_pos = positions.get(node_id)
    if (not node_pos
            or node_pos.get("pos_e") is None
            or node_pos.get("pos_n") is None
            or node_pos.get("pos_alt") is None):
        raise HTTPException(
            status_code=422,
            detail="Node has no array position — set pos_e, pos_n, pos_alt first",
        )
    if node_pos.get("pos_status") != "surveyed":
        raise HTTPException(
            status_code=422,
            detail="Node position is not marked as surveyed — update posStatus to 'surveyed' first",
        )

    ema = registry.get_gps_ema(node_id)
    if ema is None:
        raise HTTPException(
            status_code=422,
            detail="Node has no GPS EMA yet — wait for GPS to lock and settle",
        )
    ref_lat, ref_lon, ref_alt = ema["lat"], ema["lon"], ema["alt"]

    # Back-project: subtract the node's array offset from its reference coordinates.
    origin_lat = ref_lat - (node_pos["pos_n"] / status_mapper._M_PER_DEG_LAT)
    origin_lon = ref_lon - (node_pos["pos_e"] / status_mapper._M_PER_DEG_LON)
    origin_alt = ref_alt - node_pos["pos_alt"]

    set_at = _now_iso()
    await db.set_array_origin(
        lat=origin_lat,
        lon=origin_lon,
        alt_m=origin_alt,
        set_from=node_id,
        set_at=set_at,
    )
    # No node is tagged as "the" origin node any more — origin provenance
    # lives entirely in array_origin.set_from. Clear any marker left over
    # from an older build that did still tag nodes.
    await db.clear_origin()

    return ArrayOrigin(
        lat=origin_lat,
        lon=origin_lon,
        alt_m=origin_alt,
        set_from=node_id,
        set_at=set_at,
    )


@router.put("/origin", response_model=ArrayOrigin, dependencies=[Depends(require_admin)])
async def set_origin_manual(req: ArrayOriginManual):
    """Manually override the hub array origin with explicit lat/lon/alt.

    Use when the origin has been independently surveyed (e.g. via GNSS
    receiver or map reference) and does not need to be derived from a node.
    """
    set_at = _now_iso()
    await db.set_array_origin(
        lat=req.lat,
        lon=req.lon,
        alt_m=req.alt_m,
        set_from=None,
        set_at=set_at,
    )
    # Switching to a manual origin retires any node's leftover is_origin
    # marker from a previous set-from-node call.
    await db.clear_origin()
    return ArrayOrigin(lat=req.lat, lon=req.lon, alt_m=req.alt_m, set_from=None, set_at=set_at)


@router.delete("/origin", status_code=204, dependencies=[Depends(require_admin)])
async def clear_origin():
    """Clear the hub array origin and the is_origin marker on all nodes.

    After this call all node lat/lon projections become unavailable until a
    new origin is set.
    """
    await db.clear_array_origin()
    await db.clear_origin()


# ---------------------------------------------------------------------------
# Audio cleanup settings  (governs audio_cleanup.py's periodic sweep)
# ---------------------------------------------------------------------------

@router.get("/audio-cleanup-settings", response_model=AudioCleanupSettings,
            dependencies=[Depends(require_viewer)])
async def get_audio_cleanup_settings():
    """Return the current audio cleanup settings. Always available — the row
    is seeded with defaults by init_db()."""
    settings = await db.get_audio_cleanup_settings()
    return AudioCleanupSettings(
        retention_hours=settings["retention_hours"],
        max_size_bytes=settings["max_size_bytes"],
        updated_at=settings["updated_at"],
    )


@router.put("/audio-cleanup-settings", response_model=AudioCleanupSettings,
            dependencies=[Depends(require_admin)])
async def set_audio_cleanup_settings(req: AudioCleanupSettingsUpdate):
    """Update the audio cleanup settings. Takes effect on audio_cleanup.py's
    next hourly sweep — not immediately."""
    updated_at = _now_iso()
    await db.set_audio_cleanup_settings(
        retention_hours=req.retention_hours,
        max_size_bytes=req.max_size_bytes,
        updated_at=updated_at,
    )
    return AudioCleanupSettings(
        retention_hours=req.retention_hours,
        max_size_bytes=req.max_size_bytes,
        updated_at=updated_at,
    )


# ---------------------------------------------------------------------------
# Audio pull — control plane endpoints
# ---------------------------------------------------------------------------

@router.post("/audio/ack", status_code=204, dependencies=[Depends(require_node)])
async def audio_ack(body: AudioAckBody):
    """Receive a broker-forwarded node ack (ACK / DONE / UNAVAILABLE / ERROR).

    The broker calls this after hearing an AudioAckMsg from a node over ESP-NOW.
    Updates the in-memory request tracker so callers polling
    GET /audio/requests/{id} see the latest status.

    ERROR/UNAVAILABLE also resolve this pull's tdoa_attempt_nodes row, if it
    has one (found 2026-07-13 — until now this ack was logged and otherwise
    discarded, so a node that correctly reported "uplink busy" or "not in my
    ring buffer" looked identical in the Localisation UI to a node that never
    responded at all: status stuck at 'requested' forever, no error text).
    Both are genuine terminal signals from the node's own AckStatus doc
    comments — UNAVAILABLE is "do not retry", ERROR is "hub may retry" but
    won't be retried automatically today — so both end this pull the same
    way here. DONE isn't handled specially: it arrives after the node's
    actual audio POST has already landed (or is landing) via audio_push(),
    which is what actually correlates and marks 'arrived' — this ack is
    redundant with that for TDOA purposes. Dispatched via asyncio.create_task,
    not awaited, matching audio_push()'s corroboration-pull pattern — a
    broker's ack POST must never block on a DB write + solve-readiness check.
    """
    entry = _audio_requests.setdefault(body.request_id, {"acks": []})
    entry["acks"].append({
        "status": body.status,
        "srcMac": body.src_mac,
        "at": _now_iso(),
    })
    log.info("audio ack id=%s status=%s from %s", body.request_id, body.status, body.src_mac)

    if body.status in (AckStatus.ERROR, AckStatus.UNAVAILABLE):
        async def _resolve_failed_pull() -> None:
            node_row = await db.find_tdoa_attempt_node_by_request_id(body.request_id)
            if node_row is None:
                return  # not a TDOA-corroboration pull (e.g. a manual sample request)
            if node_row["node_status"] != "requested":
                return  # already resolved by another path — don't clobber it
            await db.update_tdoa_attempt_node_result(
                node_row["node_row_id"], status="push_failed",
                error=f"node ack: {body.status.value}",
            )
            log.info(
                "tdoa attempt id=%s — node=%s pull resolved as failed via ack: %s",
                node_row["attempt_id"], node_row["node_id"], body.status.value,
            )
            await _maybe_solve_tdoa_attempt(node_row["attempt_id"])
        asyncio.create_task(_resolve_failed_pull())


async def _approved_positioned_nodes() -> dict[str, tuple[float, float, float]]:
    """Return {node_id: (pos_e, pos_n, pos_alt)} for every approved,
    non-broker node with a fully-known position. This is the candidate set
    for TDOA orchestration — neighbour-selection ("initially: all") and the
    array-wide travel-time floor both start from this set. See
    species_tdoa_pipeline design notes (sound-hub/DESIGN.md) — "no
    neighbour-selection logic" gap.

    Brokers are excluded even if they happen to have a stored position
    (e.g. a node switched to broker after being surveyed, or kept for
    migration continuity — see project memory on the node-role rename):
    a broker never captures its own audio, so pulling from one only ever
    yields UNAVAILABLE, and including its position would corrupt the
    array-wide travel-time floor with a non-sensing point.
    """
    nodes = await registry.list_nodes()
    positions = await db.list_node_positions()
    out: dict[str, tuple[float, float, float]] = {}
    for node in nodes:
        if node["approval_status"] != db.APPROVED:
            continue
        live = registry.get_live_status(node["id"])
        raw_status = live.get("raw_status") or {}
        if (raw_status.get("node") or {}).get("isBroker") is True:
            continue
        pos = positions.get(node["id"])
        if pos is None:
            continue
        e, n, alt = pos.get("pos_e"), pos.get("pos_n"), pos.get("pos_alt")
        if e is None or n is None or alt is None:
            continue
        out[node["id"]] = (e, n, alt)
    return out


def _max_pairwise_distance_m(positions: dict[str, tuple[float, float, float]]) -> float:
    """Max Euclidean distance (metres) between any two of the given
    positions. Returns 0.0 if fewer than 2 positions are available — callers
    treat that as "no floor can be computed yet", not "array is a point"."""
    coords = list(positions.values())
    if len(coords) < 2:
        return 0.0
    best = 0.0
    for i in range(len(coords)):
        xi, yi, zi = coords[i]
        for j in range(i + 1, len(coords)):
            xj, yj, zj = coords[j]
            dist = math.sqrt((xi - xj) ** 2 + (yi - yj) ** 2 + (zi - zj) ** 2)
            if dist > best:
                best = dist
    return best


def _windows_overlap(
    a_start: int, a_end: int, b_start: int, b_end: int, tolerance_us: int
) -> bool:
    """True if [a_start, a_end] and [b_start, b_end] overlap once each is
    padded by tolerance_us on both sides."""
    return a_start - tolerance_us <= b_end and b_start - tolerance_us <= a_end


def _register_detection_for_tdoa(
    *,
    audio_event_id: int,
    node_id: str,
    species_key: str,
    confidence: float,
    t_start_us: int,
    t_end_us: int,
    filename: str,
) -> None:
    """Entry point called from audio_push() for every persisted detection.
    Buffers the detection into a same-species, overlapping-window cluster
    rather than planning a TDOA attempt immediately — see the module-level
    coalescing comment above _pending_clusters for why.

    filename is stashed on the member so _fire_cluster_after_delay can run
    onset detection directly against this node's own WAV (same file
    _correlate_attempt_node would later open) without an extra DB round
    trip back to audio_events at fire time.

    Synchronous: only mutates the in-memory cluster dict and, for a new
    cluster, schedules the debounce task. Never touches the DB directly —
    that all happens once the debounce elapses, in _fire_cluster_after_delay.
    """
    member = {
        "node_id": node_id,
        "audio_event_id": audio_event_id,
        "confidence": confidence,
        "t_start_us": t_start_us,
        "t_end_us": t_end_us,
        "filename": filename,
    }
    tolerance_us = TDOA_CLUSTER_OVERLAP_TOLERANCE_MS * 1000
    clusters = _pending_clusters.setdefault(species_key, [])
    for cluster in clusters:
        if any(
            _windows_overlap(
                t_start_us, t_end_us, m["t_start_us"], m["t_end_us"], tolerance_us
            )
            for m in cluster["members"]
        ):
            cluster["members"].append(member)
            log.info(
                "tdoa cluster species=%s joined by node=%s (now %d member(s))",
                species_key, node_id, len(cluster["members"]),
            )
            return

    cluster = {"members": [member], "task": None}
    cluster["task"] = asyncio.create_task(
        _fire_cluster_after_delay(species_key, cluster)
    )
    clusters.append(cluster)
    log.info(
        "tdoa cluster species=%s opened by node=%s, firing in %dms",
        species_key, node_id, TDOA_COALESCE_DEBOUNCE_MS,
    )


async def _fire_cluster_after_delay(species_key: str, cluster: dict) -> None:
    """Waits out the debounce, then plans one TDOA attempt covering every
    detection that joined this cluster. Fire-and-forget, like
    _plan_tdoa_attempt — wrapped in its own try/except since nothing awaits
    this task either.

    Origin selection requires BOTH a BirdNET detection (true of every member
    here already) AND a passing onset check — a member winning purely on
    BirdNET confidence is no longer enough to become origin, so a tdoa_attempt
    row is never created for a cluster whose members are all onset-less
    (background noise crossing the species threshold, e.g. the soundcapture171
    case this replaced: onset_failed origin that still burned 4 corroboration
    pulls). Members are tried in descending-confidence order so the
    highest-confidence *passer* wins, preserving the old tie-break behaviour
    among members that actually qualify.

    A member that fails onset here isn't discarded — it still joins
    known_reporters same as before, and gets (redundantly) onset-checked
    again inside _plan_tdoa_attempt_inner's own correlation step. That
    redundancy (and the duplicate get_effective_species_tdoa_params call) is
    accepted for now rather than threading precomputed results through
    _plan_tdoa_attempt — see conversation notes if this needs tightening.

    If nobody in the cluster passes onset, the cluster is simply dropped —
    no attempt, no pulls. There is deliberately no extended/reopened debounce
    here: a later straggler with an overlapping window won't find this
    (already-removed) cluster to join, so it opens its own fresh cluster and,
    if it independently qualifies, originates its own attempt — see
    find_open_tdoa_attempt's status filter (only 'planned'/'pulling'), which
    means a cluster that never produced a row can never be folded into.
    """
    try:
        await asyncio.sleep(TDOA_COALESCE_DEBOUNCE_MS / 1000)

        clusters = _pending_clusters.get(species_key)
        if clusters and cluster in clusters:
            clusters.remove(cluster)
            if not clusters:
                _pending_clusters.pop(species_key, None)

        members = cluster["members"]
        params, _ = await db.get_effective_species_tdoa_params(species_key)

        origin = None
        origin_arrival_us = None
        origin_ratio = None
        for m in sorted(members, key=lambda m: m["confidence"], reverse=True):
            fpath = os.path.join(_AUDIO_DIR, m["filename"])
            try:
                origin_arrival_us, origin_ratio = onset_detection.detect_onset_us(
                    params["onset_detection_method"], fpath, m["t_start_us"],
                    threshold_factor=params["onset_threshold_factor"],
                    freq_band_low_hz=params["freq_band_low_hz"],
                    freq_band_high_hz=params["freq_band_high_hz"],
                )
            except Exception as exc:
                log.info(
                    "tdoa cluster species=%s candidate node=%s failed onset "
                    "check, trying next candidate (if any): %s",
                    species_key, m["node_id"], exc,
                )
                continue
            origin = m
            break

        if origin is None:
            log.info(
                "tdoa cluster species=%s fired: none of %d member(s) passed "
                "onset check — dropping cluster, no attempt created",
                species_key, len(members),
            )
            return

        known_reporters = {
            m["node_id"]: m["audio_event_id"] for m in members if m is not origin
        }
        log.info(
            "tdoa cluster species=%s fired: origin=%s confidence=%.3f "
            "arrival_us=%.1f, %d known reporter(s)",
            species_key, origin["node_id"], origin["confidence"],
            origin_arrival_us, len(known_reporters),
        )
        await _plan_tdoa_attempt(
            audio_event_id=origin["audio_event_id"],
            origin_node_id=origin["node_id"],
            species_key=species_key,
            t_start_us=origin["t_start_us"],
            t_end_us=origin["t_end_us"],
            origin_arrival_us=origin_arrival_us,
            origin_ratio=origin_ratio,
            known_reporter_audio_events=known_reporters,
        )
    except Exception:
        log.exception(
            "tdoa cluster firing failed for species=%s", species_key
        )


async def _correlate_attempt_node(
    *,
    attempt_id: int,
    node_row_id: int,
    node_id: str,
    audio_event: dict,
    onset_detection_method: str,
    onset_threshold_factor: float,
    correlation_method: str,
    freq_band_low_hz: float | None = None,
    freq_band_high_hz: float | None = None,
) -> None:
    """TDOA orchestration milestone 3 (+ milestone 5 refinement): run onset
    detection against one node's WAV, then refine that estimate against the
    origin's WAV via leading-edge cross-correlation when possible, and
    record the outcome on its tdoa_attempt_nodes row.

    onset_threshold_factor/correlation_method/freq_band_low_hz/
    freq_band_high_hz should always be the values snapshotted onto the
    attempt's tdoa_attempts row at plan time (see _plan_tdoa_attempt_inner),
    not re-read live from species_tdoa_params — same "already-planned
    attempts keep the params they were planned with" rationale as
    onset_detection_method itself.

    audio_event is the full audio_events row for this node's contribution —
    needs 'filename' (to re-open the WAV) and 't_start_us' (to anchor the
    detected sample index to an absolute node-clock timestamp; the onset
    index is relative to the start of *this* buffer, not the attempt's
    padded pull window). Both can be missing/NULL for rows written before
    milestone 3 (filename) or from older firmware that never sent its
    actual capture window (t_start_us) — recorded as 'onset_failed' rather
    than raised, matching this function's other failure modes.

    A single node's correlation failure (missing file, unreadable WAV, no
    detectable transient) must not abort processing of the others — same
    defensive per-node style as _pull_or_reuse_one. Never raises.

    Also backfills tdoa_attempt_nodes.audio_event_id from audio_event['id']
    on every outcome (success or failure) — the 'requested' row created at
    pull time doesn't have it yet (the audio_event didn't exist until the
    push landed), so without this the row would never end up linked to the
    WAV it was actually correlated against.

    Milestone 5 (2026-07-17, see project_bird_tdoa_correlation_gap memory):
    detect_onset_us() above is run independently per node — an amplitude/
    energy-threshold detector exposed to loudness/SNR differences between
    nodes at different distances from the same call (see
    docs/tdoa-correlation-design-notes.md). After it succeeds (still the
    gate — a node with no detectable transient at all is still
    'onset_failed', unchanged), this node's estimate is refined by
    cross-correlating a short leading-edge window of its WAV against the
    origin's own WAV (correlation.correlate_leading_edge) — waveform-shape
    matching is far less sensitive to which channel is louder or carries
    more "active" signal than an independent amplitude-based detector.
    Falls back to the independent detect_onset_us() value whenever the
    origin isn't available to correlate against yet, or the correlation
    isn't trustworthy (correlation.py's peak_corr_coef/quality_ratio gate).
    """
    audio_event_id = audio_event.get("id")
    filename = audio_event.get("filename")
    t_start_us = audio_event.get("t_start_us")
    if not filename or t_start_us is None:
        error = (
            "audio_event missing filename" if not filename
            else "audio_event missing t_start_us"
        )
        await db.update_tdoa_attempt_node_result(
            node_row_id, status="onset_failed", error=error,
            audio_event_id=audio_event_id,
        )
        log.warning(
            "tdoa correlation — node=%s audio_event_id=%s: %s",
            node_id, audio_event_id, error,
        )
        return

    fpath = os.path.join(_AUDIO_DIR, filename)
    # Milestone 6 (2026-07-19, node-reported noise floor): only present when
    # audio_event came from a fresh direct pull (_save_direct_pull_audio) —
    # None for origin/known-reporter/reused_existing WAVs, which have no
    # pull-response header to read from. detect_onset_us treats None as
    # "fall back to the clip-derived median", unchanged behavior.
    node_noise_floor_rms = audio_event.get("noise_floor_rms")
    try:
        arrival_us, onset_ratio = onset_detection.detect_onset_us(
            onset_detection_method, fpath, t_start_us,
            threshold_factor=onset_threshold_factor,
            freq_band_low_hz=freq_band_low_hz,
            freq_band_high_hz=freq_band_high_hz,
            node_noise_floor_rms=node_noise_floor_rms,
        )
    except Exception as exc:
        # OnsetNotFoundError (the no-transient-found case) carries the
        # achieved ratio even on failure — see db.py's onset_ratio column
        # and OnsetNotFoundError's docstring. Other failure modes (unknown
        # method, unreadable WAV) never got far enough to compute a ratio.
        failed_ratio = getattr(exc, "ratio", None)
        await db.update_tdoa_attempt_node_result(
            node_row_id, status="onset_failed", error=str(exc),
            audio_event_id=audio_event_id, onset_ratio=failed_ratio,
        )
        log.warning(
            "tdoa correlation — node=%s onset detection failed: %s",
            node_id, exc,
        )
        return

    # Milestone 5: refine against the origin's WAV via leading-edge
    # cross-correlation when possible (see this function's docstring and
    # project_bird_tdoa_correlation_gap memory). final_arrival_us starts as
    # the independent detector's estimate and is only overwritten if a
    # trusted correlated value comes back.
    final_arrival_us = arrival_us
    peak_corr_coef = None
    quality_ratio = None
    origin = await db.get_tdoa_attempt_origin(attempt_id)
    if origin is None:
        log.info(
            "tdoa correlation — node=%s: no correlated origin available for "
            "this attempt yet, using independent onset arrival_us=%.1f",
            node_id, arrival_us,
        )
    elif origin["origin_node_id"] == node_id:
        # Shouldn't happen — _plan_tdoa_attempt_inner never calls this
        # function for the origin's own row — but guard against ever
        # correlating a node's WAV against itself.
        pass
    else:
        try:
            origin_rate, origin_data = onset_detection._load_mono(
                os.path.join(_AUDIO_DIR, origin["origin_filename"])
            )
            neighbor_rate, neighbor_data = onset_detection._load_mono(fpath)
            if freq_band_low_hz is not None and freq_band_high_hz is not None:
                origin_data = onset_detection.bandpass_filter(
                    origin_data, origin_rate, freq_band_low_hz, freq_band_high_hz
                )
                neighbor_data = onset_detection.bandpass_filter(
                    neighbor_data, neighbor_rate, freq_band_low_hz, freq_band_high_hz
                )
            corr_result = correlation.correlate_leading_edge(
                origin_data=origin_data, origin_rate=origin_rate,
                origin_t_start_us=origin["origin_t_start_us"],
                origin_arrival_us=origin["origin_arrival_us"],
                neighbor_data=neighbor_data, neighbor_rate=neighbor_rate,
                neighbor_t_start_us=t_start_us,
                method=correlation_method,
            )
        except Exception:
            corr_result = None
            log.exception(
                "tdoa correlation — node=%s leading-edge correlation crashed, "
                "keeping independent onset arrival_us=%.1f",
                node_id, arrival_us,
            )

        if corr_result is not None:
            peak_corr_coef = corr_result["peak_corr_coef"]
            quality_ratio = corr_result["quality_ratio"]
            log.info(
                "tdoa correlation — node=%s independent=%.1fus correlated=%.1fus "
                "delta=%.1fus coef=%.2f ratio=%.2f trusted=%s",
                node_id, arrival_us, corr_result["arrival_us"],
                corr_result["arrival_us"] - arrival_us, peak_corr_coef,
                quality_ratio, corr_result["trusted"],
            )
            if corr_result["trusted"]:
                final_arrival_us = corr_result["arrival_us"]

    await db.update_tdoa_attempt_node_result(
        node_row_id, status="arrived", arrival_us=final_arrival_us,
        audio_event_id=audio_event_id,
        peak_corr_coef=peak_corr_coef, quality_ratio=quality_ratio,
        onset_ratio=onset_ratio,
    )
    log.info(
        "tdoa correlation — node=%s arrival_us=%.1f (method=%s)",
        node_id, final_arrival_us, onset_detection_method,
    )


async def _fetch_audio_direct(
    node_id: str, t_start_us: int, t_end_us: int,
) -> tuple[bytes, int, int, float | None]:
    """Pull a WAV directly from a node's GET /app/api/audio/pull, bypassing
    the ESP-NOW AudioRequest/broker-relay round trip entirely (see
    AudioPullHandler.hpp on the node side, sound-capture-node).

    Replaces _issue_sample_pull for TDOA corroboration pulls only (see
    _pull_or_reuse_one below) — ESP-NOW's bare, no-retry broadcast for both
    the request and the node's ack was the root cause of nodes silently
    stuck at 'Pull requested' forever (172/174 incident, 2026-07-13); an
    ordinary HTTP GET/response has none of that failure mode — a stuck node
    now surfaces as a normal timeout. The manual POST /nodes/{id}/sample
    admin route is untouched and still goes via _issue_sample_pull/ESP-NOW;
    that route's own polling contract (GET /audio/requests/{id}) would need
    a separate, deliberate migration.

    Returns (wav_bytes, actual_start_us, actual_end_us, noise_floor_rms).
    The actual* values come from the node's X-Actual-Start-Us/
    X-Actual-End-Us response headers — AudioStore may have clipped to
    whatever it actually retained, same reason the old ESP-NOW push path
    carried these in its query string — falling back to the requested
    window if a header is missing (older firmware/unexpected response).
    noise_floor_rms comes from X-Noise-Floor-Rms (added 2026-07-19, see
    AudioPullHandler.hpp/NoiseFloorTracker.hpp) — raw int16-scale RMS, None
    if the node's firmware predates this header. See onset_detection.py's
    detect_onset_us for where this gets unit-converted and consumed.

    Raises HTTPException. status_code is deliberately partitioned so
    callers can distinguish "the node told us no" from "we couldn't even
    ask": 404/503 are the node's own response (window unavailable / capture
    not running); everything else (424/502/504) means the request itself
    never got a real answer from the node.
      424 — node not in registry, no known IP yet, or relay client not
            initialised (nothing was sent)
      404 — node responded 404 (window unavailable — ring evicted, SD gap,
            or snapshot slot busy)
      503 — node responded 503 (capture not running — broker role, or
            AudioCapture failed to start)
      502 — node responded with any other unexpected status
      504 — request timed out, or the node was unreachable (connection
            refused/reset, DNS failure, etc.)
    """
    node = await registry.get_node(node_id)
    if node is None or not node.get("ip_address"):
        raise HTTPException(
            status_code=424, detail="Node not found or IP not yet known",
        )
    if _relay_client is None:
        raise HTTPException(status_code=424, detail="Relay client not initialised")

    # Plain HTTP, not config.NODE_SCHEME (https) — this endpoint runs on its
    # own dedicated, unauthenticated plain-HTTP server (AudioPullServer,
    # sound-capture-node) on a separate port, kept off the node's HTTPS UI
    # server so a long pull can't block page loads there. See
    # AudioPullServer's class comment (sound-capture-node/main/app).
    pull_port = getattr(config, "NODE_AUDIO_PULL_PORT", 8080)
    url = (
        f"http://{node['ip_address']}:{pull_port}/app/api/audio/pull"
        f"?tStartUs={t_start_us}&tEndUs={t_end_us}"
    )
    try:
        resp = await _relay_client.get(
            url, timeout=getattr(config, "AUDIO_PULL_TIMEOUT_S", 15.0),
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail=f"pull timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=504, detail=f"node unreachable: {exc}") from exc

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=resp.text or "window unavailable")
    if resp.status_code == 503:
        raise HTTPException(status_code=503, detail=resp.text or "capture not running")
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502, detail=f"node returned HTTP {resp.status_code}",
        )

    actual_start_us = int(resp.headers.get("X-Actual-Start-Us", t_start_us))
    actual_end_us = int(resp.headers.get("X-Actual-End-Us", t_end_us))
    noise_floor_raw = resp.headers.get("X-Noise-Floor-Rms")
    noise_floor_rms = float(noise_floor_raw) if noise_floor_raw is not None else None
    return resp.content, actual_start_us, actual_end_us, noise_floor_rms


async def _save_direct_pull_audio(
    node_id: str, wav_bytes: bytes, t_start_us: int, t_end_us: int,
    noise_floor_rms: float | None = None,
) -> dict:
    """Save a directly-pulled WAV and record its audio_events row, mirroring
    the purpose == "tdoa_corroboration" branch of audio_push() below.
    BirdNET re-analysis is always skipped here — a direct pull exists
    purely to corroborate a species the origin detection already
    identified, so re-running the model on it would be wasted CPU (same
    reasoning that branch documents).

    Filenamed audio_pull_{node_id}_{t_start_us}.wav — tStartUs-keyed per
    the 2026-07-12 filename-collision fix (see audio_push's docstring). No
    srcMac component: a direct pull has no MAC anywhere in its path (one
    fewer moving part than the ESP-NOW path needed — no wifiMac lookup, no
    broker resolution).

    noise_floor_rms (added 2026-07-19) — the node's reported floor for this
    pull (see _fetch_audio_direct). Passed straight through to
    _correlate_attempt_node via the returned dict, same as before. Also now
    persisted to the audio_events row itself (added 2026-07-25 — see
    db.insert_audio_event's noise_floor_rms param) so real field noise-floor
    readings accumulate somewhere queryable instead of only existing in a
    hub log line for this one detect_onset_us() call — the intended
    consumer is per-species window_margin_pre_ms derivation (see project
    memory on the margin/transit-time discussion), not built yet.

    Returns a dict shaped the way _correlate_attempt_node expects
    (id/filename/t_start_us/t_end_us/noise_floor_rms) — not a full
    audio_events DB row.
    """
    os.makedirs(_AUDIO_DIR, exist_ok=True)
    fname = f"audio_pull_{node_id}_{t_start_us}.wav"
    fpath = os.path.join(_AUDIO_DIR, fname)
    with open(fpath, "wb") as fh:
        fh.write(wav_bytes)

    audio_event_id = await db.insert_audio_event(
        node_id=node_id, triggered=False, received_at=_now_iso(),
        bytes_=len(wav_bytes), analysis_status="skipped_birdnet_tdoa_pull",
        t_start_us=t_start_us, t_end_us=t_end_us, filename=fname,
        noise_floor_rms=noise_floor_rms,
    )
    log.info("direct pull node=%s — %d bytes -> %s (noise_floor_rms=%s)",
              node_id, len(wav_bytes), fname, noise_floor_rms)
    return {
        "id": audio_event_id, "filename": fname,
        "t_start_us": t_start_us, "t_end_us": t_end_us,
        "noise_floor_rms": noise_floor_rms,
    }


async def _maybe_solve_tdoa_attempt(attempt_id: int) -> None:
    """TDOA orchestration milestone 4: check whether a tdoa_attempts row now
    has enough correlated arrivals to solve, and if so, solve + persist.

    Called from several places — inline during planning (origin/known/reused
    nodes correlated eagerly, see _plan_tdoa_attempt_inner) and from
    audio_push() once a freshly-pulled WAV is correlated (dispatched via
    asyncio.create_task there, same fire-and-forget reasoning as
    _plan_tdoa_attempt: nothing should block a node's push response on a
    solver call). Wrapped in its own try/except for the same reason —
    an exception here must never propagate into a caller that isn't
    expecting one.
    """
    try:
        await _maybe_solve_tdoa_attempt_inner(attempt_id)
    except Exception:
        log.exception("TDOA solve check failed for attempt_id=%s", attempt_id)


async def _maybe_solve_tdoa_attempt_inner(attempt_id: int) -> None:
    """Actual solve-readiness check and solve for _maybe_solve_tdoa_attempt
    — see that function's docstring."""
    attempt = await db.get_tdoa_attempt(attempt_id)
    if attempt is None or attempt["status"] not in ("planned", "pulling"):
        # Already solved/failed by a previous call (multiple nodes can
        # finish correlating close together), or the attempt no longer
        # exists — nothing to do.
        return

    nodes = await db.list_tdoa_attempt_nodes(attempt_id)
    arrived = [n for n in nodes if n["status"] == "arrived" and n["arrival_us"] is not None]

    # min_corroborating_nodes counts the origin as one of the nodes toward
    # this total (see _plan_tdoa_attempt_inner's origin_counts) — it is NOT
    # "origin plus this many corroborators". With the field's own default of
    # 4, that means a 4-node quadratic solve (mirror-root ambiguous, see
    # tdoa_solver.py) fires by default, not the unambiguous 5+-node
    # least-squares case — raise min_corroborating_nodes to 5 for that.
    min_corroborating_nodes = attempt["min_corroborating_nodes"]
    if len(arrived) < min_corroborating_nodes:
        # Not enough arrivals yet — but if enough of the *rest* have already
        # terminally failed (request_failed/onset_failed/push_failed) that
        # min_corroborating_nodes can no longer be reached even if every
        # still-pending node comes good, this attempt is dead: fail it now
        # instead of leaving it at 'pulling' forever waiting on corroborators
        # that already told the hub they're never arriving (see audio_ack's
        # push_failed handling, added 2026-07-13 alongside this check — the
        # two are meant to work together: without this, marking individual
        # nodes failed still left the attempt itself stuck).
        terminal_failed = [
            n for n in nodes
            if n["status"] in ("request_failed", "onset_failed", "push_failed")
        ]
        still_possible = len(nodes) - len(terminal_failed)
        if still_possible < min_corroborating_nodes:
            await db.update_tdoa_attempt_status(
                attempt_id, "failed",
                failure_reason=(
                    f"only {len(arrived)} node(s) arrived and {len(terminal_failed)} "
                    f"failed — at most {still_possible} could still arrive, need "
                    f">= {min_corroborating_nodes}"
                ),
            )
            log.info(
                "tdoa attempt id=%s — failed: %d arrived, %d failed, only %d "
                "could still arrive < min_corroborating_nodes=%d",
                attempt_id, len(arrived), len(terminal_failed), still_possible,
                min_corroborating_nodes,
            )
        return  # still waiting on more nodes to correlate (or just failed above)

    positions = await db.list_node_positions()
    solver_nodes: list[TdoaNode] = []
    timestamps_us: list[float] = []
    for row in arrived:
        pos = positions.get(row["node_id"])
        if pos is None or any(pos.get(k) is None for k in ("pos_e", "pos_n", "pos_alt")):
            # Shouldn't happen — every arrived node came from
            # _approved_positioned_nodes()'s candidate set at planning time
            # — but a position can change between planning and correlation
            # (re-survey, node removed). Guard rather than let a stale
            # lookup crash the solve.
            continue
        solver_nodes.append(TdoaNode(
            node_id=row["node_id"], x=pos["pos_e"], y=pos["pos_n"], z=pos["pos_alt"],
        ))
        timestamps_us.append(row["arrival_us"])

    if len(solver_nodes) < min_corroborating_nodes:
        log.warning(
            "tdoa attempt id=%s — %d node(s) arrived but only %d have usable "
            "positions right now, below min_corroborating_nodes=%d — not "
            "solving yet",
            attempt_id, len(arrived), len(solver_nodes), min_corroborating_nodes,
        )
        return

    # hint_point (2026-07-23): centroid of the nodes actually contributing
    # to this solve. Resolves the 4-node quadratic path's mirror-root
    # ambiguity (tdoa_solver._select_root) — without a hint, that path
    # falls back to "prefer the root farther from the array centroid",
    # which its own docstring says fails for sources close to the array.
    # Every real source here is close to the array (deck-clustered nodes),
    # so that fallback was liable to pick the unphysical mirror root even
    # given perfectly good input data — confirmed 2026-07-23 against real
    # solved attempts with clean pairwise-consistent timestamps that still
    # landed thousands of km away (see project_bird_tdoa_implausible_solves
    # memory). The centroid is a simple, always-available "somewhere near
    # the array" prior — no per-species/per-attempt config needed. Only
    # affects root *selection*; the solver still returns both candidates
    # (ambiguous_root) so a bad pick is still visible after the fact.
    cx = sum(n.x for n in solver_nodes) / len(solver_nodes)
    cy = sum(n.y for n in solver_nodes) / len(solver_nodes)
    cz = sum(n.z for n in solver_nodes) / len(solver_nodes)
    try:
        result = tdoa_solve(
            nodes=solver_nodes, timestamps_us=timestamps_us,
            speed_of_sound=DEFAULT_SPEED_OF_SOUND,
            hint_point=(cx, cy, cz),
        )
    except ValueError as exc:
        await db.update_tdoa_attempt_status(
            attempt_id, "failed", failure_reason=f"solver failed: {exc}",
        )
        log.warning("tdoa attempt id=%s — solver failed: %s", attempt_id, exc)
        return

    ambiguous_json = None
    if result.ambiguous_root is not None:
        ambiguous_json = json.dumps(list(result.ambiguous_root[:3]))

    await db.persist_tdoa_solution(
        attempt_id, e=result.x, n=result.y, alt=result.z,
        residual_m=result.residual, method=result.method,
        ambiguous_root_json=ambiguous_json,
    )
    log.info(
        "tdoa attempt id=%s — solved: E=%.2f N=%.2f Alt=%.2f residual=%.3fm "
        "method=%s nodes=%d%s",
        attempt_id, result.x, result.y, result.z, result.residual,
        result.method, len(solver_nodes),
        " (ambiguous root stored, resolved via array-centroid hint_point)" if ambiguous_json else "",
    )


async def _plan_tdoa_attempt(
    *,
    audio_event_id: int,
    origin_node_id: str | None,
    species_key: str,
    t_start_us: int,
    t_end_us: int,
    origin_arrival_us: float,
    origin_ratio: float | None = None,
    known_reporter_audio_events: dict[str, int] | None = None,
) -> None:
    """TDOA orchestration milestones 1+2: on a persisted top-species
    detection, look up the species' params, compute the (travel-time-floored)
    pull window, pick candidate neighbour nodes, record the plan as a
    tdoa_attempts row, then issue the actual pull to each candidate neighbour
    and record per-node outcomes (milestone 2). Milestone 3 (correlating
    WAVs already on hand — origin, known reporters, reused-existing
    neighbours — back to this attempt) also happens inline inside
    _plan_tdoa_attempt_inner, since those WAVs already exist by planning
    time; freshly-*pulled* WAVs are correlated later, from audio_push(),
    when they land.

    origin_arrival_us is the onset arrival time _fire_cluster_after_delay
    already established for this origin (a passing onset check is now a
    precondition for calling this function at all — see that function's
    docstring). The pull window sent to neighbours is centered on this
    instant, not on the origin's raw t_start_us/t_end_us detection span —
    t_start_us/t_end_us are kept only to log/sanity-check that arrival
    actually falls inside the capture window it was found in.

    origin_ratio (added 2026-07-25) is that same onset check's achieved
    peak_envelope/background ratio — threaded through purely to persist
    onto the origin's own tdoa_attempt_nodes row (see onset_ratio in
    db.py), not used in any planning decision here.

    known_reporter_audio_events maps node_id -> audio_event_id for other
    nodes that detection-coalescing (_register_detection_for_tdoa /
    _fire_cluster_after_delay) already grouped with this origin as the same
    underlying acoustic event. Those nodes are known corroborators before
    planning even starts and must not be pulled again.

    Dispatched from _fire_cluster_after_delay via asyncio.create_task —
    fire-and-forget, not awaited — so a slow or unreachable neighbour can
    never delay the HTTP response to the node whose push triggered this (see
    project_soundhub_congestion notes: this used to run inline before the
    push response returned). Because nothing awaits this task, the whole
    body is wrapped in its own try/except below rather than relying on a
    caller to catch it — an unhandled exception in a detached task would
    otherwise only surface as an "exception was never retrieved" warning.
    A per-node pull failure (one neighbour unreachable) is caught
    individually inside _pull_or_reuse_one and does not abort pulls to the
    remaining neighbours.
    """
    try:
        await _plan_tdoa_attempt_inner(
            audio_event_id=audio_event_id,
            origin_node_id=origin_node_id,
            species_key=species_key,
            t_start_us=t_start_us,
            t_end_us=t_end_us,
            origin_arrival_us=origin_arrival_us,
            origin_ratio=origin_ratio,
            known_reporter_audio_events=known_reporter_audio_events or {},
        )
    except Exception:
        log.exception(
            "TDOA attempt planning failed for audio_event_id=%s species=%s",
            audio_event_id, species_key,
        )


async def _plan_tdoa_attempt_inner(
    *,
    audio_event_id: int,
    origin_node_id: str | None,
    species_key: str,
    t_start_us: int,
    t_end_us: int,
    origin_arrival_us: float,
    origin_ratio: float | None,
    known_reporter_audio_events: dict[str, int],
) -> None:
    """Actual planning logic for _plan_tdoa_attempt — see that function's
    docstring. Split out so the outer function can wrap this in a single
    try/except without an extra indent level across the whole body."""
    params, used_default = await db.get_effective_species_tdoa_params(species_key)

    if not (t_start_us <= origin_arrival_us <= t_end_us):
        # Shouldn't happen — arrival_us came from detect_onset_us searching
        # exactly this capture window in _fire_cluster_after_delay — but a
        # bogus arrival silently recentering the pull window on the wrong
        # instant would be far worse than a loud warning here.
        log.warning(
            "tdoa planning — species=%s origin=%s arrival_us=%.1f falls "
            "outside its own capture window [%d,%d]us — proceeding anyway",
            species_key, origin_node_id, origin_arrival_us, t_start_us, t_end_us,
        )

    candidates = await _approved_positioned_nodes()
    # Worst-case (slowest-plausible, cold-day) speed of sound here, not the
    # solver's DEFAULT_SPEED_OF_SOUND — this floor only ever needs to widen
    # the pull window to be safe, never to be accurate. See tdoa_solver.py's
    # WORST_CASE_SPEED_OF_SOUND docstring.
    travel_time_floor_s = _max_pairwise_distance_m(candidates) / WORST_CASE_SPEED_OF_SOUND
    min_corroborating_nodes = params["min_corroborating_nodes"]

    # Additive, not max() (changed 2026-07-17 — see
    # project_bird_tdoa_correlation_gap memory). The two terms mean different
    # things: window_margin_pre/post_ms is slop around the origin's own onset
    # detection (algorithmic/SNR uncertainty in *where* the transient is),
    # while travel_time_floor_s is the geometric worst case for *how far* a
    # neighbour's true arrival can sit from the origin's. max()-combining them
    # meant that once travel_time_floor_s exceeded the configured margin, the
    # margin was entirely superseded rather than adding any real slop — on
    # this property travel_time_floor_s is usually the larger of the two, so
    # the species-tunable margin was silently doing nothing most of the time.
    # Adding them guarantees both are respected simultaneously. There is
    # deliberately no separate pull_window_s floor on top of this anymore —
    # removed the same day (see [[feedback_prefers_clean_removal]]): a fixed
    # 3.0s minimum was pulling roughly 2x more audio than this property's
    # geometry (~3Ha, worst-case diagonal travel time well under 1s) ever
    # required, and it silently overrode the margin+floor computation below
    # whenever that computation came out smaller than 3.0s total.
    margin_pre_s = (params["window_margin_pre_ms"] / 1000.0) + travel_time_floor_s
    margin_post_s = (params["window_margin_post_ms"] / 1000.0) + travel_time_floor_s
    # Recentred on the origin's onset arrival instant rather than its raw
    # (BirdNET-classification-driven) t_start_us/t_end_us span — the span
    # can be a second or more wide and off-center from the actual transient,
    # whereas arrival_us is the precise instant every other node's copy of
    # the same acoustic event should cluster around.
    pull_t_start_us = int(origin_arrival_us - margin_pre_s * 1e6)
    pull_t_end_us = int(origin_arrival_us + margin_post_s * 1e6)

    # Straggler safety net: this detection's own debounce cluster
    # (_register_detection_for_tdoa) already fired and reached here without
    # finding a sibling for it — e.g. a slower ESP-NOW relay hop landed it
    # after the cluster's 100ms window closed. Rather than plan a second
    # full attempt for the same event and re-pull neighbours another attempt
    # is already pulling, fold this origin (and anything coalesced with it)
    # into whatever attempt is already in flight for this species+window.
    existing = await db.find_open_tdoa_attempt(species_key, pull_t_start_us, pull_t_end_us)
    if existing is not None:
        stragglers = dict(known_reporter_audio_events)
        if origin_node_id is not None:
            stragglers[origin_node_id] = audio_event_id
        for nid, aeid in stragglers.items():
            await db.insert_tdoa_attempt_node(
                attempt_id=existing["id"], node_id=nid, request_id=None,
                status="reused_existing", audio_event_id=aeid,
            )
        log.info(
            "tdoa attempt id=%s — species=%s window=[%d,%d]us already in "
            "flight, attached %d straggler(s) instead of planning a new "
            "attempt: %s",
            existing["id"], species_key, pull_t_start_us, pull_t_end_us,
            len(stragglers), list(stragglers),
        )
        return

    known_reporters = {
        nid: aeid for nid, aeid in known_reporter_audio_events.items()
        if nid in candidates
    }
    planned_node_ids = [
        nid for nid in candidates
        if nid != origin_node_id and nid not in known_reporters
    ]
    # origin_node_id and known_reporters are excluded from planned_node_ids
    # (they already have audio for this event, not pull targets) but both
    # still count toward the corroborating total *if* they're approved +
    # positioned — otherwise their arrival can't be used in the solve.
    origin_counts = 1 if origin_node_id in candidates else 0
    known_counts = len(known_reporters)

    attempt_id = await db.insert_tdoa_attempt(
        audio_event_id=audio_event_id,
        origin_node_id=origin_node_id,
        species_key=species_key,
        used_default=used_default,
        status="planned",
        t_start_us=pull_t_start_us,
        t_end_us=pull_t_end_us,
        planned_node_ids=json.dumps(planned_node_ids),
        min_corroborating_nodes=params["min_corroborating_nodes"],
        correlation_method=params["correlation_method"],
        onset_detection_method=params["onset_detection_method"],
        onset_threshold_factor=params["onset_threshold_factor"],
        freq_band_low_hz=params["freq_band_low_hz"],
        freq_band_high_hz=params["freq_band_high_hz"],
        travel_time_floor_s=travel_time_floor_s,
    )
    log.info(
        "tdoa attempt id=%s planned: species=%s origin=%s used_default=%s "
        "known_reporters=%s neighbours=%s window=[%d,%d]us floor=%.3fs",
        attempt_id, species_key, origin_node_id, used_default,
        list(known_reporters), planned_node_ids, pull_t_start_us, pull_t_end_us,
        travel_time_floor_s,
    )

    # Milestone 3: the origin's own WAV (the push that triggered planning)
    # already exists — give it a tdoa_attempt_nodes row too (status='origin'
    # initially) and record its arrival, rather than only ever recording
    # origin_node_id on the attempt row itself. Folding the origin into this
    # table means milestone 4's corroborating-node count is one uniform
    # query over tdoa_attempt_nodes instead of origin/known/pulled being
    # counted three different ways. Only if the origin is itself
    # approved+positioned (origin_counts==1) — an unpositioned origin can
    # never contribute a usable arrival time to the solve.
    #
    # No _correlate_attempt_node call here (unlike known_reporters/pulled
    # neighbours below): origin_arrival_us was already established by
    # _fire_cluster_after_delay as the precondition for planning this attempt
    # at all, so re-running onset detection against the same WAV here would
    # just repeat work for the same deterministic answer. Recorded directly
    # as 'arrived' rather than re-deriving it.
    if origin_counts and origin_node_id is not None:
        origin_row_id = await db.insert_tdoa_attempt_node(
            attempt_id=attempt_id, node_id=origin_node_id, request_id=None,
            status="origin", audio_event_id=audio_event_id,
        )
        await db.update_tdoa_attempt_node_result(
            origin_row_id, status="arrived", arrival_us=origin_arrival_us,
            audio_event_id=audio_event_id, onset_ratio=origin_ratio,
        )

    for nid, aeid in known_reporters.items():
        node_row_id = await db.insert_tdoa_attempt_node(
            attempt_id=attempt_id, node_id=nid, request_id=None,
            status="reused_existing", audio_event_id=aeid,
        )
        # Known reporters' WAVs already exist (grouped into this origin's
        # debounce cluster before planning even started, or a straggler
        # attached above) — correlate now instead of waiting on a push that
        # will never arrive for these.
        known_audio_event = await db.get_audio_event(aeid)
        if known_audio_event is not None:
            await _correlate_attempt_node(
                attempt_id=attempt_id,
                node_row_id=node_row_id, node_id=nid,
                audio_event=known_audio_event,
                onset_detection_method=params["onset_detection_method"],
                onset_threshold_factor=params["onset_threshold_factor"],
                correlation_method=params["correlation_method"],
                freq_band_low_hz=params["freq_band_low_hz"],
                freq_band_high_hz=params["freq_band_high_hz"],
            )
        else:
            await db.update_tdoa_attempt_node_result(
                node_row_id, status="onset_failed",
                error=f"audio_event id={aeid} not found",
            )

    # If the array doesn't have enough approved+positioned nodes to ever
    # satisfy min_corroborating_nodes, issuing pulls would just leave the
    # attempt stuck at 'pulling' forever (the solver can never run). Fail
    # fast instead of wasting pulls on neighbours that can't help.
    total_possible = len(planned_node_ids) + origin_counts + known_counts
    if total_possible < min_corroborating_nodes:
        await db.update_tdoa_attempt_status(
            attempt_id, "failed",
            failure_reason=(
                f"only {total_possible} approved+positioned node(s) available "
                f"(incl. origin + known reporters), need >= {min_corroborating_nodes} — "
                f"skipping pulls"
            ),
        )
        log.info(
            "tdoa attempt id=%s — failed before pulling: %d available, "
            "need >= %d",
            attempt_id, total_possible, min_corroborating_nodes,
        )
        return

    # Per-pair transit time (added 2026-07-25 — see project memory on the
    # margin/transit-time discussion). travel_time_floor_s above is the
    # *property-wide* worst case (max distance between any two
    # approved+positioned nodes) — correct as the attempt-level envelope
    # (pull_t_start_us/pull_t_end_us, used for the tdoa_attempts row and
    # find_open_tdoa_attempt's straggler-matching), but overkill for any
    # individual pull to a neighbour closer than the property's farthest
    # pair. The actual audio requested from a specific neighbour only needs
    # to cover that neighbour's own worst-case transit time from the
    # origin, not the whole network's. NodeTransit_pair is always <=
    # travel_time_floor_s by construction (it's a distance between two of
    # the same candidates _max_pairwise_distance_m already maximised over),
    # so every per-node pull window computed below is guaranteed to sit
    # inside the attempt-level envelope — no schema or straggler-matching
    # change needed, only the actual bytes requested per pull shrink.
    origin_position = candidates.get(origin_node_id) if origin_node_id is not None else None

    def _pull_window_for_node(node_id: str) -> tuple[int, int]:
        """(t_start_us, t_end_us) to request from this specific neighbour.
        Falls back to the property-wide travel_time_floor_s (same as the
        attempt-level window) whenever a pair-specific distance can't be
        computed — origin unpositioned/unapproved is the only case that
        should hit this in practice, since every node_id passed in here
        comes from planned_node_ids, itself derived from `candidates`, so
        the neighbour's own position is always known."""
        node_position = candidates.get(node_id)
        if origin_position is None or node_position is None:
            transit_s = travel_time_floor_s
        else:
            ox, oy, oz = origin_position
            nx, ny, nz = node_position
            dist_m = math.sqrt((ox - nx) ** 2 + (oy - ny) ** 2 + (oz - nz) ** 2)
            transit_s = dist_m / WORST_CASE_SPEED_OF_SOUND
        node_margin_pre_s = (params["window_margin_pre_ms"] / 1000.0) + transit_s
        node_margin_post_s = (params["window_margin_post_ms"] / 1000.0) + transit_s
        return (
            int(origin_arrival_us - node_margin_pre_s * 1e6),
            int(origin_arrival_us + node_margin_post_s * 1e6),
        )

    async def _pull_or_reuse_one(nid: str) -> bool:
        """Resolve one remaining candidate neighbour. If it already pushed
        (or was pulled for a different attempt) audio covering the window
        this attempt needs, reuse that instead of pulling again — this is
        the main defence against duplicate pulls when several nodes detect
        the same event but miss each other's debounce cluster (e.g. two
        different top-species calls at once — see design discussion).
        Otherwise issues a fresh pull. Returns True if the node now has (or
        will have) audio available — i.e. counts toward the corroborating
        total."""
        node_pull_start_us, node_pull_end_us = _pull_window_for_node(nid)
        existing_event = await db.find_covering_audio_event(
            nid, node_pull_start_us, node_pull_end_us
        )
        if existing_event is not None:
            node_row_id = await db.insert_tdoa_attempt_node(
                attempt_id=attempt_id, node_id=nid, request_id=None,
                status="reused_existing", audio_event_id=existing_event["id"],
            )
            log.info(
                "tdoa attempt id=%s — node=%s already has covering "
                "audio_event id=%s, skipping pull",
                attempt_id, nid, existing_event["id"],
            )
            # WAV already exists (find_covering_audio_event only returns
            # events whose window fully contains what this attempt needs) —
            # correlate now instead of waiting on a push that won't come.
            await _correlate_attempt_node(
                attempt_id=attempt_id,
                node_row_id=node_row_id, node_id=nid,
                audio_event=existing_event,
                onset_detection_method=params["onset_detection_method"],
                onset_threshold_factor=params["onset_threshold_factor"],
                correlation_method=params["correlation_method"],
                freq_band_low_hz=params["freq_band_low_hz"],
                freq_band_high_hz=params["freq_band_high_hz"],
            )
            return True
        # Direct hub->node HTTP pull (see _fetch_audio_direct) — replaces the
        # old ESP-NOW AudioRequest/broker-relay round trip for this path.
        # Synchronous: the WAV is in hand (or the failure is known) by the
        # time this returns, so there is no more 'requested' status waiting
        # on a push/ack that may never arrive — every outcome below is
        # terminal on the first attempt.
        try:
            wav_bytes, actual_start_us, actual_end_us, noise_floor_rms = await _fetch_audio_direct(
                nid, node_pull_start_us, node_pull_end_us,
            )
        except HTTPException as exc:
            # 404/503 are the node's own response (it heard the request and
            # explicitly said no) — push_failed. Everything else (424/502/
            # 504) means the request never got a real answer from the node
            # at all — request_failed. See _fetch_audio_direct's docstring.
            node_status = "push_failed" if exc.status_code in (404, 503) else "request_failed"
            await db.insert_tdoa_attempt_node(
                attempt_id=attempt_id, node_id=nid, request_id=None,
                status=node_status, error=str(exc.detail),
            )
            log.warning(
                "tdoa attempt id=%s — direct pull to node=%s failed (%s): %s",
                attempt_id, nid, exc.status_code, exc.detail,
            )
            return False

        audio_event = await _save_direct_pull_audio(
            nid, wav_bytes, actual_start_us, actual_end_us,
            noise_floor_rms=noise_floor_rms,
        )
        node_row_id = await db.insert_tdoa_attempt_node(
            attempt_id=attempt_id, node_id=nid, request_id=None,
            status="pulled", audio_event_id=audio_event["id"],
        )
        # WAV is already on disk — correlate immediately, same as the
        # reused_existing/known_reporters/origin branches above, instead of
        # waiting for a push that will never come.
        await _correlate_attempt_node(
            attempt_id=attempt_id,
            node_row_id=node_row_id, node_id=nid,
            audio_event=audio_event,
            onset_detection_method=params["onset_detection_method"],
            onset_threshold_factor=params["onset_threshold_factor"],
            correlation_method=params["correlation_method"],
            freq_band_low_hz=params["freq_band_low_hz"],
            freq_band_high_hz=params["freq_band_high_hz"],
        )
        return True

    # Fan out to all candidate neighbours concurrently rather than one at a
    # time — see project_soundhub_congestion notes: this used to be a
    # sequential loop, so N neighbours meant N round-trips stacked back to
    # back inside a single audio_push request.
    results = await asyncio.gather(*(_pull_or_reuse_one(nid) for nid in planned_node_ids))
    resolved = sum(1 for ok in results if ok)

    # Per-node failures (caught above) can shrink the final corroborating
    # count below min_corroborating_nodes even though the pre-flight check
    # passed — re-check against the actual resolved count, not just
    # "resolved > 0", before deciding 'pulling' vs 'failed'.
    final_count = resolved + origin_counts + known_counts
    if final_count >= min_corroborating_nodes:
        await db.update_tdoa_attempt_status(attempt_id, "pulling")
    else:
        await db.update_tdoa_attempt_status(
            attempt_id, "failed",
            failure_reason=(
                "no neighbour nodes available" if not planned_node_ids
                else (
                    f"only {final_count} node(s) succeeded (incl. origin + "
                    f"known reporters), need >= {min_corroborating_nodes}"
                )
            ),
        )
    log.info(
        "tdoa attempt id=%s — pulls/reuses resolved for %d/%d remaining neighbours",
        attempt_id, resolved, len(planned_node_ids),
    )

    # Milestone 4: origin + known reporters + reused-existing neighbours were
    # all correlated inline above (their WAVs already existed) — if that
    # alone already reached min_corroborating_nodes, nothing will ever call
    # back into this attempt again (fresh pulls, if any were issued, are the
    # only other thing that can), so check now rather than only from
    # audio_push() when a pull lands.
    await _maybe_solve_tdoa_attempt(attempt_id)


# Caps concurrent BirdNET analysis pipelines dispatched from audio_push()'s
# fire-and-forget path below. Necessary companion to the 2026-07-17
# fire-and-forget change: that fix removed the accidental backpressure where
# every node's blocked HTTP request naturally limited how much concurrent
# hub-side analysis work could ever exist at once. Without a cap, a large
# trigger burst can dispatch far more concurrent work than the box can
# service — a node170 log the same day showed the hub itself return HTTP 502
# and then a multi-minute stretch of outright connection failures for both
# audio pushes and heartbeats, not just the targeted push route (see
# project_soundhub_congestion notes). _analyzer_lock (birdnet_worker.py)
# already guarantees only one analysis actually runs at a time; this bounds
# how many pipelines can be queued waiting for a turn, so excess work waits
# cheaply on this semaphore instead of piling up. Value picked conservatively
# for a single NUC-class box, not from a measured throughput ceiling —
# revisit if dawn/dusk chorus load still saturates it.
_MAX_CONCURRENT_ANALYSES = 2
_analysis_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_ANALYSES)


@router.post("/audio/push", status_code=200, dependencies=[Depends(require_node)])
async def audio_push(
    request: Request,
    requestId: int | None = Query(
        None,
        description="Matches the originating AudioRequestMsg.requestId. "
                     "Absent for node-initiated (untriggered-by-hub) pushes.",
    ),
    srcMac: str    = Query(..., description="Wi-Fi STA MAC of the sending node (xx:xx:xx:xx:xx:xx)"),
    nodeId: str | None = Query(
        None,
        description="Sending node's hostname — matches nodes.id. Authoritative "
                     "identity for this push; srcMac is kept for filenames/debugging.",
    ),
    tStartUs: int | None = Query(
        None,
        description="Actual capture-window start, node-clock Unix epoch µs — "
                     "AudioStore::Snapshot.actualStartUs, not the originally "
                     "requested window. Sent by the node for both triggered "
                     "pushes and hub-requested pulls, since either can be "
                     "clipped to whatever audio the node actually retained. "
                     "Optional only for backward compatibility with older "
                     "firmware that doesn't send it.",
    ),
    tEndUs: int | None = Query(
        None,
        description="Actual capture-window end, node-clock Unix epoch µs. See tStartUs.",
    ),
):
    """Receive a WAV audio segment pushed directly from a node.

    Two cases:
      - Hub-initiated: node POSTs here after receiving an AUDIO_REQUEST and
        successfully retrieving the segment. requestId is present and
        matches the originating ack-tracking entry.
      - Node-initiated (future): a node's own trigger fires and it pushes
        without ever being asked. requestId is absent.

    The file is saved to the audio/ directory as
    audio_{requestId}_{srcMac}.wav for a hub-initiated pull (requestId is an
    effectively-unique random int per pull — see _issue_sample_pull), or
    audio_{nodeId}_{tStartUs}_{srcMac}.wav for a node-initiated push, keyed
    by the capture's actual start microsecond rather than bare nodeId.
    Without tStartUs, every self-trigger from the same node would silently
    overwrite the previous one's file on disk (found 2026-07-12) — the old
    audio_events row would still point at that filename, but its contents
    would now belong to a different, later capture window, corrupting any
    later onset-detection/TDOA correlation or download against it. Falls
    back to bare nodeId (today's collision-prone behavior, no worse than
    before) only if tStartUs is absent — older firmware that doesn't send
    it. BirdNET analysis then runs against the saved file in a thread pool
    (model inference is blocking) and any detections are persisted tagged
    with this node.  Analysis failures are logged, not raised — a node's
    push is the thing being acknowledged here, not the success of analysis.

    Exception: a push whose requestId was issued by _plan_tdoa_attempt
    purely for TDOA corroboration (purpose="tdoa_corroboration" in
    _audio_requests) skips BirdNET analysis entirely — the species is
    already known from the origin detection that triggered the pull, so
    re-running the model would just be wasted CPU. This mattered enough to
    fix: with min_corroborating_nodes=4 (default), every planned attempt was
    quietly costing up to 4 extra full analyses on top of the one that
    mattered — see project_soundhub_congestion notes.
    """
    data = await request.body()
    os.makedirs(_AUDIO_DIR, exist_ok=True)
    if requestId is not None:
        label = str(requestId)
    elif tStartUs is not None:
        label = f"{nodeId or 'unknown'}_{tStartUs}"
    else:
        label = nodeId or "untriggered"
    fname = f"audio_{label}_{srcMac.replace(':', '')}.wav"
    fpath = os.path.join(_AUDIO_DIR, fname)
    with open(fpath, "wb") as fh:
        fh.write(data)
    purpose = None
    if requestId is not None:
        entry = _audio_requests.setdefault(requestId, {"acks": []})
        entry.update({"file": fname, "bytes": len(data), "savedAt": _now_iso(), "nodeId": nodeId})
        purpose = entry.get("purpose")
    log.info("audio push id=%s node=%s from %s — %d bytes → %s",
              requestId, nodeId, srcMac, len(data), fname)

    triggered = requestId is None

    if purpose == "tdoa_corroboration":
        log.info(
            "audio push id=%s node=%s — TDOA-corroboration pull, skipping "
            "BirdNET re-analysis (species already known from origin detection)",
            requestId, nodeId,
        )
        audio_event_id = await db.insert_audio_event(
            node_id=nodeId, triggered=triggered, received_at=_now_iso(),
            bytes_=len(data), analysis_status="skipped_birdnet_tdoa_pull",
            t_start_us=tStartUs, t_end_us=tEndUs, filename=fname,
        )
        # Milestone 3: match this arriving pull back to the
        # tdoa_attempt_nodes row _plan_tdoa_attempt_inner created when it
        # issued the pull, then correlate + check whether the attempt can
        # now solve. tStartUs is required to anchor the onset sample index
        # to an absolute timestamp — older firmware that doesn't send it
        # leaves the node_row at 'requested' forever (a known gap, not
        # solved here). Dispatched via asyncio.create_task, not awaited —
        # same congestion-avoidance reasoning as _plan_tdoa_attempt: a node's
        # push response must never block on onset detection or a solve.
        if nodeId is not None and tStartUs is not None:
            node_row = await db.find_tdoa_attempt_node_by_request_id(requestId)
            if node_row is not None:
                async def _correlate_and_maybe_solve() -> None:
                    await _correlate_attempt_node(
                        attempt_id=node_row["attempt_id"],
                        node_row_id=node_row["node_row_id"], node_id=nodeId,
                        audio_event={
                            "id": audio_event_id, "filename": fname,
                            "t_start_us": tStartUs, "t_end_us": tEndUs,
                        },
                        onset_detection_method=node_row["onset_detection_method"],
                        onset_threshold_factor=node_row["onset_threshold_factor"],
                        correlation_method=node_row["correlation_method"],
                        freq_band_low_hz=node_row["freq_band_low_hz"],
                        freq_band_high_hz=node_row["freq_band_high_hz"],
                    )
                    await _maybe_solve_tdoa_attempt(node_row["attempt_id"])
                asyncio.create_task(_correlate_and_maybe_solve())
            else:
                log.warning(
                    "audio push id=%s node=%s — tdoa_corroboration pull with "
                    "no matching tdoa_attempt_nodes row (hub restarted since "
                    "the pull was issued?)",
                    requestId, nodeId,
                )
        return

    if not birdnet_worker.ready():
        log.warning("audio push id=%s node=%s — BirdNET not yet loaded, skipping analysis",
                     requestId, nodeId)
        await db.insert_audio_event(
            node_id=nodeId, triggered=triggered, received_at=_now_iso(),
            bytes_=len(data), analysis_status="skipped_not_ready",
            t_start_us=tStartUs, t_end_us=tEndUs, filename=fname,
        )
        return

    # BirdNET analysis (and everything downstream of it) is dispatched as a
    # detached background task so the node's push response returns right
    # after the file is saved, instead of blocking on model inference —
    # see project_soundhub_congestion notes. Mirrors the tdoa_corroboration
    # branch above, which already does this for its own (skipped-analysis)
    # path. Exceptions must be caught inside the task itself — same
    # reasoning as _plan_tdoa_attempt's wrapper split: once detached,
    # nothing awaits this coroutine to propagate an error.
    async def _analyze_and_register() -> None:
        # Acquired for the whole pipeline (analysis + persistence), not just
        # the run_in_executor call — the goal is capping how many of these
        # background pipelines are actively doing real work at once, not
        # just how many are inside the executor. Waiting here is a cheap
        # asyncio-level await, not a blocked OS thread, so excess bursts
        # queue without spinning up more executor threads than necessary.
        async with _analysis_semaphore:
            try:
                loop = asyncio.get_event_loop()
                # analyze_wav_full runs at min_conf=0.0 so we can see the best
                # candidate even if it falls below the persisted-detection
                # threshold — see audio_events.top_confidence/top_species.
                raw = await loop.run_in_executor(
                    None, functools.partial(birdnet_worker.analyze_wav_full, fpath, use_geo=True),
                )
            except Exception:
                log.exception("audio push id=%s node=%s — BirdNET analysis failed", requestId, nodeId)
                await db.insert_audio_event(
                    node_id=nodeId, triggered=triggered, received_at=_now_iso(),
                    bytes_=len(data), analysis_status="error",
                    t_start_us=tStartUs, t_end_us=tEndUs, filename=fname,
                )
                return

            persisted = [d for d in raw if d.get("confidence", 0.0) >= birdnet_worker.DEFAULT_MIN_CONF]
            top = max(raw, key=lambda d: d.get("confidence", 0.0)) if raw else None

            if persisted:
                await db.insert_detections(fname, _now_iso(), persisted, node_id=nodeId)
                log.info("audio push id=%s node=%s — %d detection(s) registered",
                          requestId, nodeId, len(persisted))
                # Fire-and-forget: resolve Wikipedia/eBird links for any
                # species in this batch never seen before. Never blocks this
                # request — see species_links.maybe_resolve_new().
                species_links.maybe_resolve_new(persisted)

            audio_event_id = await db.insert_audio_event(
                node_id=nodeId, triggered=triggered, received_at=_now_iso(),
                bytes_=len(data), analysis_status="analyzed",
                detection_count=len(persisted),
                top_confidence=top.get("confidence") if top else None,
                top_species=top.get("common_name") if top else None,
                t_start_us=tStartUs, t_end_us=tEndUs, filename=fname,
            )

            # TDOA orchestration milestone 1 (species_tdoa_pipeline design,
            # sound-hub/DESIGN.md): a persisted top-species detection with a
            # known capture window and node identity registers into the
            # detection-coalescing buffer. tStartUs/tEndUs absent means older
            # firmware that doesn't send the actual capture window yet —
            # nothing to anchor a pull window on. nodeId absent means the push
            # couldn't be attributed to a known node — nothing to record as
            # origin/reporter. Either way, planning is skipped for this push.
            #
            # _register_detection_for_tdoa does not plan immediately: it
            # buffers into a short debounce (TDOA_COALESCE_DEBOUNCE_MS) so that
            # several nodes detecting the same call within milliseconds of each
            # other collapse into one planned attempt instead of each firing
            # its own — see the module-level coalescing comment near
            # _pending_clusters and project_soundhub_tdoa_dedup notes. This
            # used to call asyncio.create_task(_plan_tdoa_attempt(...)) directly
            # here.
            if persisted and tStartUs is not None and tEndUs is not None and nodeId is not None:
                _register_detection_for_tdoa(
                    audio_event_id=audio_event_id,
                    node_id=nodeId,
                    species_key=top["common_name"],
                    confidence=top.get("confidence", 0.0),
                    t_start_us=tStartUs,
                    t_end_us=tEndUs,
                    filename=fname,
                )

    asyncio.create_task(_analyze_and_register())


@router.get("/audio/requests/{request_id}", dependencies=[Depends(require_viewer)])
async def get_audio_request(request_id: int):
    """Poll the status of an audio pull request.

    Returns the ack history and, once the push has arrived, the saved filename
    and byte count.  Resets on hub restart (Phase 1 — intentional).
    """
    entry = _audio_requests.get(request_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Request not found")
    return {"requestId": request_id, **entry}


async def _issue_sample_pull(
    node_id: str, t_start_us: int, t_end_us: int,
    *, purpose: str = "manual",
) -> int:
    """Trigger an audio pull from a node for a specific UTC time range.
    Shared by the POST /nodes/{id}/sample route and the TDOA orchestration
    hook (_plan_tdoa_attempt) — extracted so both go through identical
    mac/broker resolution and _audio_requests bookkeeping rather than two
    copies of the same relay logic.

    purpose tags the resulting _audio_requests entry — "manual" (default,
    operator-triggered via /nodes/{id}/sample) or "tdoa_corroboration"
    (issued by _plan_tdoa_attempt purely to get a neighbour's waveform for
    TDOA cross-correlation). audio_push() reads this back to decide whether
    the arriving push needs a full BirdNET re-analysis — a corroboration
    pull already knows its species from the origin detection, so re-running
    the model on it is pure wasted CPU. See project_soundhub_congestion
    notes for why this matters under TDOA load.

    Flow:
      1. Resolves the target node's wifiMac from its last polled status.
      2. Finds the currently reachable broker node.
      3. POSTs the AudioRequestMsg JSON to the broker's /espnow/relay endpoint.
      4. Returns the new requestId immediately — poll
         GET /audio/requests/{requestId} for ack and push progress.

    Raises HTTPException:
      404 — node not found in registry
      422 — node wifiMac not yet known (wait for next status poll)
      503 — no reachable broker, or broker IP unknown, or relay client not ready
      502 — broker relay endpoint returned an error
    """
    node = await registry.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")

    # Resolve the target node's WiFi STA MAC from its last polled status.
    live      = registry.get_live_status(node_id)
    raw       = live.get("raw_status") or {}
    node_info = raw.get("node", {})
    target_mac = node_info.get("wifiMac")
    if not target_mac:
        raise HTTPException(
            status_code=422,
            detail="Node wifiMac not yet known — wait for next status poll",
        )

    # Find the first reachable broker node.
    broker_ip = None
    for n, lv, _ in await _mapped_nodes():
        r = (lv.get("raw_status") or {}).get("node", {})
        if r.get("isBroker") is True and lv.get("reachable"):
            broker_ip = n.get("ip_address")
            break
    if broker_ip is None:
        raise HTTPException(status_code=503, detail="No reachable broker node found")

    if _relay_client is None:
        raise HTTPException(status_code=503, detail="Relay client not initialised")

    import random
    request_id = random.randint(1, 2**31 - 1)
    _audio_requests[request_id] = {
        "acks": [],
        "targetMac": target_mac,
        "createdAt": _now_iso(),
        "purpose": purpose,
        # Recorded at issue time (not just once the push arrives, as
        # "nodeId"/"file" etc. are below in audio_push) so an in-flight pull
        # can eventually be matched against its target node/window without
        # waiting for the response — groundwork for de-duplicating
        # concurrently-outstanding pulls; not yet consumed anywhere.
        "nodeId": node_id,
        "tStartUs": t_start_us,
        "tEndUs": t_end_us,
    }

    payload = {
        "requestId": request_id,
        "targetMac": target_mac,
        "tStartUs":  t_start_us,
        "tEndUs":    t_end_us,
        "hubIp":     config.BASE_STATION_IP,
        "hubPort":   config.BASE_STATION_PORT,
    }

    r = await _relay_client.post(f"{config.NODE_SCHEME}://{broker_ip}/espnow/relay", json=payload)

    if r.status_code not in (200, 202):
        raise HTTPException(
            status_code=502,
            detail=f"Broker relay rejected: HTTP {r.status_code}",
        )

    log.info("audio pull relayed -- id=%s node=%s mac=%s [%s ... %s]",
             request_id, node_id, target_mac, t_start_us, t_end_us)
    return request_id


@router.post("/nodes/{node_id}/sample", status_code=202, dependencies=[Depends(require_admin)])
async def request_sample(node_id: str, req: AudioSampleRequest):
    """Trigger an audio pull from a node for a specific UTC time range.
    See _issue_sample_pull for the actual mechanics; this route is a thin
    wrapper shaping its result into the documented response body.
    """
    request_id = await _issue_sample_pull(node_id, req.t_start_us, req.t_end_us)
    return {"requestId": request_id, "status": "relayed"}


# ---------------------------------------------------------------------------
# Audio analytics — diagnostic visibility into the trigger/BirdNET pipeline
# ---------------------------------------------------------------------------

@router.get("/analytics/audio", response_model=AudioAnalytics, dependencies=[Depends(require_viewer)])
async def audio_analytics(
    limit: int = Query(default=200, ge=1, le=2000),
    node_id: str | None = Query(default=None, alias="nodeId"),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    time_of_day: str | None = Query(default=None, alias="timeOfDay", pattern="^(dawn|dusk|daytime|nighttime)$"),
):
    """Return audio-push event history + per-node summary stats.

    Every push to POST /api/audio/push is recorded in audio_events
    regardless of BirdNET outcome — this is the only place that shows
    pushes which never produced a persisted detection (BirdNET ran but
    scored everything below threshold, or hadn't loaded yet, or errored).
    Pairs with GET /api/detections, which only ever shows the hits.

    `summary` is one row per node_id (per-node stat cards); `events` is the
    raw push history (recent-events table), both filtered the same way by
    node_id/from/to.

    time_of_day follows the same convention as GET /detections: classified
    per-row against the array's sun-relative dawn/dusk/daytime/nighttime
    windows (see suntimes.py), since each event's window depends on its own
    calendar date and can't be expressed as a SQL range. That also means
    `summary` can't use its normal SQL GROUP BY path under time_of_day (same
    reasoning as GET /detections/species-summary) — it re-aggregates the
    classified rows in Python instead, capped at the same 2000-row ceiling
    GET /detections uses.
    """
    if not time_of_day:
        raw_events = await db.list_audio_events(limit=limit, node_id=node_id, from_ts=from_ts, to_ts=to_ts)
        raw_summary = await db.audio_event_summary(from_ts=from_ts, to_ts=to_ts)
        if node_id:
            raw_summary = [r for r in raw_summary if r["node_id"] == node_id]

        summary = [
            NodeAudioSummary(
                node_id=r["node_id"],
                total_pushes=r["total_pushes"],
                triggered_pushes=r["triggered_pushes"] or 0,
                pushes_with_detections=r["pushes_with_detections"] or 0,
                pushes_zero_detections=r["pushes_zero_detections"] or 0,
                detection_rate=(r["pushes_with_detections"] or 0) / r["total_pushes"] if r["total_pushes"] else 0.0,
                last_push_at=r["last_push_at"],
                last_trigger_at=r["last_trigger_at"],
                avg_near_miss_confidence=r["avg_near_miss_confidence"],
            )
            for r in raw_summary
        ]
        events = [
            AudioEventRecord(
                id=r["id"], node_id=r["node_id"], triggered=bool(r["triggered"]),
                received_at=r["received_at"], bytes=r["bytes"],
                analysis_status=r["analysis_status"], detection_count=r["detection_count"],
                top_confidence=r["top_confidence"], top_species=r["top_species"],
                t_start_us=r["t_start_us"], t_end_us=r["t_end_us"],
            )
            for r in raw_events
        ]
        return AudioAnalytics(summary=summary, events=events)

    origin = await db.get_array_origin()
    if origin is None:
        raise HTTPException(
            status_code=400,
            detail="Array origin not set — configure the array's reference lat/lon before filtering by time of day",
        )

    raw = await db.list_audio_events(limit=2000, node_id=node_id, from_ts=from_ts, to_ts=to_ts)
    buckets = suntimes.classify_many(
        (datetime.fromisoformat(r["received_at"]) for r in raw), origin["lat"], origin["lon"],
    )
    matched = [r for r, bucket in zip(raw, buckets) if bucket == time_of_day]

    # Re-aggregate per node — same fields/logic as audio_event_summary()'s
    # SQL, just computed over the already-classified rows in Python.
    agg: dict[str, dict] = {}
    for r in matched:
        entry = agg.setdefault(r["node_id"], {
            "node_id": r["node_id"], "total_pushes": 0, "triggered_pushes": 0,
            "pushes_with_detections": 0, "pushes_zero_detections": 0,
            "last_push_at": r["received_at"], "last_trigger_at": None,
            "_conf_sum": 0.0, "_conf_count": 0,
        })
        entry["total_pushes"] += 1
        if r["triggered"]:
            entry["triggered_pushes"] += 1
            if entry["last_trigger_at"] is None or r["received_at"] > entry["last_trigger_at"]:
                entry["last_trigger_at"] = r["received_at"]
        if r["detection_count"] > 0:
            entry["pushes_with_detections"] += 1
        else:
            entry["pushes_zero_detections"] += 1
            if r["top_confidence"] is not None:
                entry["_conf_sum"] += r["top_confidence"]
                entry["_conf_count"] += 1
        if r["received_at"] > entry["last_push_at"]:
            entry["last_push_at"] = r["received_at"]

    summary = [
        NodeAudioSummary(
            node_id=v["node_id"],
            total_pushes=v["total_pushes"],
            triggered_pushes=v["triggered_pushes"],
            pushes_with_detections=v["pushes_with_detections"],
            pushes_zero_detections=v["pushes_zero_detections"],
            detection_rate=(v["pushes_with_detections"] / v["total_pushes"]) if v["total_pushes"] else 0.0,
            last_push_at=v["last_push_at"],
            last_trigger_at=v["last_trigger_at"],
            avg_near_miss_confidence=(v["_conf_sum"] / v["_conf_count"]) if v["_conf_count"] else None,
        )
        for v in agg.values()
    ]
    summary.sort(key=lambda s: s.total_pushes, reverse=True)

    events = [
        AudioEventRecord(
            id=r["id"], node_id=r["node_id"], triggered=bool(r["triggered"]),
            received_at=r["received_at"], bytes=r["bytes"],
            analysis_status=r["analysis_status"], detection_count=r["detection_count"],
            top_confidence=r["top_confidence"], top_species=r["top_species"],
            t_start_us=r["t_start_us"], t_end_us=r["t_end_us"],
        )
        for r in matched[:limit]
    ]
    return AudioAnalytics(summary=summary, events=events)


@router.get("/analytics/trigger-diag", response_model=TriggerDiagAnalytics, dependencies=[Depends(require_viewer)])
async def trigger_diag_analytics(
    limit: int = Query(default=500, ge=1, le=5000),
    node_id: str | None = Query(default=None, alias="nodeId"),
    fired_only: bool = Query(default=False, alias="firedOnly"),
):
    """Return trigger-diagnostic event history + per-node summary stats.

    Pulled from each node's GET /app/api/trigger-diag by the poller and
    persisted to trigger_events — see TriggerDiagnostics.hpp on the node
    side. Built to diagnose the v2 dual-gate trigger (band energy + spectral
    flux): a node only ever records "interesting" blocks (a gate ratio
    >= 1.5, or an actual fire), so this view answers "how close did a near-
    miss get, and on which gate" — the data the field-report species
    collapse (pied butcherbird / lorikeet / cockatoo no longer firing) needs
    before any threshold gets retuned.
    """
    raw_events = await db.list_trigger_events(limit=limit, node_id=node_id, fired_only=fired_only)
    raw_summary = await db.trigger_event_summary()
    if node_id:
        raw_summary = [r for r in raw_summary if r["node_id"] == node_id]

    summary = [
        NodeTriggerSummary(
            node_id=r["node_id"],
            total_rows=r["total_rows"],
            fired_rows=r["fired_rows"] or 0,
            near_miss_rows=r["near_miss_rows"] or 0,
            avg_energy_ratio=r["avg_energy_ratio"],
            avg_flux_ratio=r["avg_flux_ratio"],
            last_t_us=r["last_t_us"],
        )
        for r in raw_summary
    ]
    events = [
        TriggerEventRecord(
            id=r["id"], node_id=r["node_id"], t_us=r["t_us"],
            energy_ratio=r["energy_ratio"], flux_ratio=r["flux_ratio"],
            fired=bool(r["fired"]),
        )
        for r in raw_events
    ]
    return TriggerDiagAnalytics(summary=summary, events=events)


@router.get(
    "/analytics/trigger-diag/histogram",
    response_model=TriggerHistogramResponse,
    dependencies=[Depends(require_viewer)],
)
async def trigger_diag_histogram(
    since_us: int = Query(alias="sinceUs"),
    until_us: int = Query(alias="untilUs"),
    node_id: str | None = Query(default=None, alias="nodeId"),
    bucket_width: float = Query(default=1.0, alias="bucketWidth", gt=0),
    max_ratio: float = Query(default=20.0, alias="maxRatio", gt=0),
):
    """Distribution of trigger-diagnostic ratios over an explicit time range.

    Complements /analytics/trigger-diag: that endpoint's raw block list
    turned out to answer "did it fire recently" poorly during a sustained
    call (near-miss floods bury the fire under identical rows sharing a
    LIMIT with every other node). This answers "where does the near-miss
    distribution sit relative to the fire threshold" instead — the question
    that actually matters for a retuning decision — by bucketing ratios
    server-side rather than shipping raw rows to the client.

    Scoped to raw trigger_events, so since_us can't reach further back than
    TRIGGER_EVENTS_RETENTION_HOURS lets rows survive. No default time range
    is applied — the caller must reason about the retention window.
    """
    raw = await db.trigger_ratio_histogram(
        node_id=node_id, since_us=since_us, until_us=until_us,
        bucket_width=bucket_width, max_ratio=max_ratio,
    )

    def _to_histogram(rows: list[dict]) -> RatioHistogram:
        return RatioHistogram(
            bucket_width=bucket_width,
            max_ratio=max_ratio,
            buckets=[
                RatioHistogramBucket(
                    bucket_start=r["bucket"] * bucket_width,
                    count=r["count"],
                    fired_count=r["fired_count"] or 0,
                )
                for r in rows
            ],
        )

    return TriggerHistogramResponse(
        node_id=node_id, since_us=since_us, until_us=until_us,
        energy=_to_histogram(raw["energy"]), flux=_to_histogram(raw["flux"]),
    )


@router.get(
    "/analytics/trigger-diag/rollups",
    response_model=TriggerRollupResponse,
    dependencies=[Depends(require_viewer)],
)
async def trigger_diag_rollups(
    node_id: str | None = Query(default=None, alias="nodeId"),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    time_of_day: str | None = Query(default=None, alias="timeOfDay", pattern="^(dawn|dusk|daytime|nighttime)$"),
    limit: int = Query(default=50_000, ge=1, le=200_000),
):
    """Per-minute trigger activity over time, from trigger_event_rollups.

    Answers a question the ratio histogram can't: does a given fire sit
    inside a plausible burst of real activity, or isolated with nothing
    around it? Rollups are never pruned (unlike raw trigger_events), so this
    can look back days or weeks, not just the raw retention window.

    from/to/time_of_day follow the same convention as GET /detections:
    time_of_day is classified per-bucket against the array's sun-relative
    dawn/dusk/daytime/nighttime windows (see suntimes.py) rather than
    expressed as a SQL range, since each bucket's window depends on its own
    local calendar date.
    """
    # datetime.fromisoformat() only accepts a trailing "Z" from Python 3.11
    # onward — the browser's Date.toISOString() (what the frontend sends)
    # always produces one, so normalize to a "+00:00" offset defensively
    # rather than assuming the deployed Python version.
    def _parse_iso_us(ts: str) -> int:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1_000_000)

    from_us = _parse_iso_us(from_ts) if from_ts else None
    to_us = _parse_iso_us(to_ts) if to_ts else None

    rows = await db.list_trigger_rollups(node_id=node_id, from_us=from_us, to_us=to_us, limit=limit)

    if time_of_day:
        origin = await db.get_array_origin()
        if origin is None:
            raise HTTPException(
                status_code=400,
                detail="Array origin not set — configure the array's reference lat/lon before filtering by time of day",
            )
        timestamps = (datetime.fromtimestamp(r["bucket_start_us"] / 1_000_000, tz=timezone.utc) for r in rows)
        buckets = suntimes.classify_many(timestamps, origin["lat"], origin["lon"])
        rows = [r for r, bucket in zip(rows, buckets) if bucket == time_of_day]

    return TriggerRollupResponse(buckets=[
        TriggerRollupBucket(
            node_id=r["node_id"], bucket_start_us=r["bucket_start_us"],
            entry_count=r["entry_count"], fired_count=r["fired_count"],
            energy_ratio_min=r["energy_ratio_min"], energy_ratio_avg=r["energy_ratio_avg"],
            energy_ratio_max=r["energy_ratio_max"], flux_ratio_min=r["flux_ratio_min"],
            flux_ratio_avg=r["flux_ratio_avg"], flux_ratio_max=r["flux_ratio_max"],
        )
        for r in rows
    ])


# ---------------------------------------------------------------------------
# TDOA solver
# ---------------------------------------------------------------------------

@router.post("/tdoa/solve", response_model=TdoaResponse, dependencies=[Depends(require_viewer)])
async def solve_tdoa(req: TdoaRequest):
    """Solve for an acoustic source position given GPS-timestamped arrivals.

    Looks up each node's position from the hub's node_positions table.
    Returns the solved (E, N, Alt) position in metres from the array origin,
    plus the RMS residual and (for 4-node solves) the mirror root.

    Errors:
      422 -- fewer than 4 timestamps, unknown node ID, node has no stored position
      500 -- solver failure (degenerate geometry)
    """
    if len(req.timestamps) < 4:
        raise HTTPException(status_code=422, detail="At least 4 node timestamps required")

    positions = await db.list_node_positions()

    solver_nodes: list[TdoaNode] = []
    timestamps_us: list[float] = []
    for ts in req.timestamps:
        node_pos = positions.get(ts.node_id)
        if node_pos is None:
            raise HTTPException(
                status_code=422,
                detail=f"Node '{ts.node_id}' has no stored position",
            )
        if any(node_pos.get(k) is None for k in ("pos_e", "pos_n", "pos_alt")):
            raise HTTPException(
                status_code=422,
                detail=f"Node '{ts.node_id}' position is incomplete (pos_e/pos_n/pos_alt required)",
            )
        solver_nodes.append(TdoaNode(
            node_id=ts.node_id,
            x=node_pos["pos_e"],
            y=node_pos["pos_n"],
            z=node_pos["pos_alt"],
        ))
        timestamps_us.append(ts.timestamp_us)

    try:
        result = tdoa_solve(
            nodes=solver_nodes,
            timestamps_us=timestamps_us,
            speed_of_sound=req.speed_of_sound,
            hint_point=req.hint_point,
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"Solver failure: {exc}")

    ambiguous: tuple[float, float, float] | None = None
    if result.ambiguous_root is not None:
        ax, ay, az, _ = result.ambiguous_root
        ambiguous = (ax, ay, az)

    return TdoaResponse(
        x=result.x,
        y=result.y,
        z=result.z,
        residual_m=result.residual,
        method=result.method,
        ambiguous_root=ambiguous,
    )


def _tdoa_attempt_node_record_from_row(
    row: dict,
    *,
    origin_arrival_us: float | None,
    baseline_m: float | None,
) -> TdoaAttemptNodeRecord:
    """Build a TdoaAttemptNodeRecord from a tdoa_attempt_nodes DB row — as
    returned by db.list_tdoa_attempt_nodes(), which already LEFT JOINs in
    audio_events.filename.

    origin_arrival_us/baseline_m (2026-07-23, Analysis > Localisation UI
    request): let the frontend show the actual TDOA fed to the solver and a
    live physical-plausibility check, rather than needing a one-off script
    against the DB every time — see project_bird_tdoa_implausible_solves
    memory for why this was worth surfacing directly in the UI.
    """
    delta_from_origin_us = None
    consistency_ratio = None
    if row["arrival_us"] is not None and origin_arrival_us is not None:
        delta_from_origin_us = row["arrival_us"] - origin_arrival_us
        if baseline_m is not None and baseline_m > 0:
            implied_range_diff_m = DEFAULT_SPEED_OF_SOUND * delta_from_origin_us * 1e-6
            consistency_ratio = abs(implied_range_diff_m) / baseline_m

    return TdoaAttemptNodeRecord(
        id=row["id"],
        node_id=row["node_id"],
        request_id=row["request_id"],
        status=row["status"],
        arrival_us=row["arrival_us"],
        error=row["error"],
        audio_event_id=row["audio_event_id"],
        filename=row["filename"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        peak_corr_coef=row["peak_corr_coef"],
        quality_ratio=row["quality_ratio"],
        delta_from_origin_us=delta_from_origin_us,
        baseline_m=baseline_m,
        consistency_ratio=consistency_ratio,
    )


def _tdoa_attempt_record_from_row(
    row: dict, node_rows: list[dict], positions: dict[str, dict],
) -> TdoaAttemptRecord:
    """Build a TdoaAttemptRecord from a tdoa_attempts row plus its already-
    fetched tdoa_attempt_nodes rows. solve_ambiguous_json is stored as a
    JSON array string (see db.persist_tdoa_solution) — parsed back to a
    tuple here, the same shape solve_tdoa() above returns ad-hoc.

    positions: node_id -> {pos_e, pos_n, pos_alt} for every surveyed node
    (db.list_node_positions(), fetched once by the caller — not per
    attempt) — used to compute each node's baseline distance to the
    origin for the UI's physical-plausibility column.
    """
    ambiguous = None
    if row.get("solve_ambiguous_json"):
        parsed = json.loads(row["solve_ambiguous_json"])
        ambiguous = (parsed[0], parsed[1], parsed[2])

    origin_node_id = row["origin_node_id"]
    origin_row = next((n for n in node_rows if n["node_id"] == origin_node_id), None)
    origin_arrival_us = origin_row["arrival_us"] if origin_row else None
    origin_pos = positions.get(origin_node_id) if origin_node_id else None

    def _baseline_to_origin(node_id: str) -> float | None:
        pos = positions.get(node_id)
        if pos is None or origin_pos is None:
            return None
        keys = ("pos_e", "pos_n", "pos_alt")
        if any(pos.get(k) is None or origin_pos.get(k) is None for k in keys):
            return None
        return math.sqrt(
            (pos["pos_e"] - origin_pos["pos_e"]) ** 2
            + (pos["pos_n"] - origin_pos["pos_n"]) ** 2
            + (pos["pos_alt"] - origin_pos["pos_alt"]) ** 2
        )

    return TdoaAttemptRecord(
        id=row["id"],
        audio_event_id=row["audio_event_id"],
        origin_node_id=row["origin_node_id"],
        species_key=row["species_key"],
        used_default=bool(row["used_default"]),
        status=row["status"],
        t_start_us=row["t_start_us"],
        t_end_us=row["t_end_us"],
        min_corroborating_nodes=row["min_corroborating_nodes"],
        correlation_method=row["correlation_method"],
        onset_detection_method=row["onset_detection_method"],
        onset_threshold_factor=row["onset_threshold_factor"],
        freq_band_low_hz=row["freq_band_low_hz"],
        freq_band_high_hz=row["freq_band_high_hz"],
        travel_time_floor_s=row["travel_time_floor_s"],
        failure_reason=row["failure_reason"],
        solved_e=row["solved_e"],
        solved_n=row["solved_n"],
        solved_alt=row["solved_alt"],
        solve_residual_m=row["solve_residual_m"],
        solve_method=row["solve_method"],
        solve_ambiguous_root=ambiguous,
        solved_at=row["solved_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        nodes=[
            _tdoa_attempt_node_record_from_row(
                n,
                origin_arrival_us=origin_arrival_us,
                baseline_m=_baseline_to_origin(n["node_id"]),
            )
            for n in node_rows
        ],
    )


@router.get(
    "/tdoa/attempts",
    response_model=list[TdoaAttemptRecord],
    dependencies=[Depends(require_viewer)],
)
async def list_tdoa_attempts(limit: int = Query(default=50, ge=1, le=500)):
    """List recent TDOA orchestration attempts (species_tdoa_pipeline
    design, sound-hub/DESIGN.md), newest first, each with its per-node
    correlation rows embedded.

    Surfaces milestones 1-4 end to end: plan (candidate neighbours, window),
    execution (pull/reuse per node), correlation (arrival_us per node), and
    solve result (or failure reason) — previously DB-inspectable only.

    One query per attempt to fetch its nodes (db.list_tdoa_attempt_nodes) —
    fine at this project's scale (a handful of nodes, infrequent
    detections); would need batching if attempt volume grows much beyond
    that. node_positions is fetched once for the whole response, not per
    attempt — it's small (one row per node) and every attempt needs it for
    the per-node baseline/consistency-ratio fields.
    """
    attempts = await db.list_tdoa_attempts(limit=limit)
    positions = await db.list_node_positions()
    records = []
    for attempt in attempts:
        node_rows = await db.list_tdoa_attempt_nodes(attempt["id"])
        records.append(_tdoa_attempt_record_from_row(attempt, node_rows, positions))
    return records


@router.get("/tdoa/audio/{filename}", dependencies=[Depends(require_viewer)])
async def get_tdoa_audio(filename: str):
    """Serve one WAV file from the hub's audio/ directory by filename —
    backs the Localisation sub-tab's "File" column, e.g. to manually
    sanity-check a node's audio behind an onset-detection failure.

    filename always comes from a DB-stored value in the UI's normal flow
    (audio_events.filename, joined in by db.list_tdoa_attempt_nodes), but
    it's still a request parameter the browser controls — validated against
    path traversal regardless, rather than trusted just because the normal
    caller is well-behaved. Two layers: os.path.basename() strips any
    directory components (rejects anything containing a slash outright),
    and the resolved path is confirmed to still be inside audio/ (catches
    a bare '..' or '.', which basename() alone does not — basename('..')
    is '..').

    Auth note: viewer-level like the rest of the TDOA read surface, but
    this repo authenticates with a Bearer header (auth.js), not cookies —
    a plain <a href> wouldn't carry it, and putting the token in the URL
    instead was deliberately rejected (browser history / server access
    logs). The frontend fetches this via apiFetch() as a blob and triggers
    the download client-side instead of linking directly.
    """
    safe_name = os.path.basename(filename)
    if safe_name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    audio_root = os.path.realpath(_AUDIO_DIR)
    fpath = os.path.realpath(os.path.join(audio_root, safe_name))
    if not fpath.startswith(audio_root + os.sep):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="Audio file not found")

    return FileResponse(fpath, media_type="audio/wav", filename=safe_name)


async def _delete_tdoa_attempts_and_files(attempt_ids: list[int]) -> dict:
    """Shared core for both delete routes below: removes the given
    tdoa_attempts/tdoa_attempt_nodes rows (+ orphaned corroboration
    audio_events, see db.delete_tdoa_attempts's docstring for exactly what
    counts as safe to drop), then unlinks the corresponding WAV files from
    disk. DB deletion and file unlinking are deliberately two separate
    steps — a file already gone (or a permissions hiccup) shouldn't be
    treated as a reason to fail the whole request; the DB is the source of
    truth here, so a partial-file-cleanup outcome is logged, not raised.
    """
    result = await db.delete_tdoa_attempts(attempt_ids)
    audio_files_deleted = 0
    for fname in result["filenames_deleted"]:
        fpath = os.path.join(_AUDIO_DIR, fname)
        try:
            os.remove(fpath)
            audio_files_deleted += 1
        except OSError as exc:
            log.warning("tdoa attempt cleanup — failed to remove %s: %s", fpath, exc)
    log.info(
        "tdoa attempts deleted: %d attempt(s), %d audio file(s) — ids=%s",
        result["attempts_deleted"], audio_files_deleted, attempt_ids,
    )
    return {
        "attemptsDeleted": result["attempts_deleted"],
        "audioFilesDeleted": audio_files_deleted,
    }


@router.delete("/tdoa/attempts", dependencies=[Depends(require_admin)])
async def delete_tdoa_attempts_in_range(
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
):
    """Bulk-delete TDOA attempts (+ their corroboration-only audio) with
    created_at in [from, to]. Both bounds are optional but at least one is
    required — an unbounded "delete everything" is almost certainly a
    mistake, not an intended bulk action, so it's rejected outright rather
    than silently doing the maximal thing.

    Built for clearing bench-test noise (nodes physically co-located but
    configured with their eventual field positions, producing 'solved'
    attempts with meaningless coordinates — see the 2026-07-15 discussion)
    without losing real BirdNET detection history from the same period —
    see _delete_tdoa_attempts_and_files/db.delete_tdoa_attempts for exactly
    what is and isn't touched.
    """
    if from_ts is None and to_ts is None:
        raise HTTPException(
            status_code=422,
            detail="At least one of 'from' or 'to' is required",
        )
    attempt_ids = await db.resolve_tdoa_attempt_ids_in_range(from_ts, to_ts)
    return await _delete_tdoa_attempts_and_files(attempt_ids)


@router.delete("/tdoa/attempts/{attempt_id}", dependencies=[Depends(require_admin)])
async def delete_tdoa_attempt(attempt_id: int):
    """Delete a single TDOA attempt (+ its corroboration-only audio) —
    the per-row counterpart to the bulk range-delete above, for pruning one
    bad result without needing a date boundary that cleanly separates it
    from attempts you want to keep."""
    return await _delete_tdoa_attempts_and_files([attempt_id])


# ---------------------------------------------------------------------------
# Species TDOA params (CRUDable — see SpeciesTdoaParams docstring for why)
# ---------------------------------------------------------------------------

def _species_tdoa_params_record_from_row(row: dict) -> SpeciesTdoaParamsRecord:
    """Build a SpeciesTdoaParamsRecord from a species_tdoa_params DB row."""
    return SpeciesTdoaParamsRecord(
        species_key=row["species_key"],
        enabled=bool(row["enabled"]),
        correlation_method=row["correlation_method"],
        onset_detection_method=row["onset_detection_method"],
        onset_threshold_factor=row["onset_threshold_factor"],
        freq_band_low_hz=row["freq_band_low_hz"],
        freq_band_high_hz=row["freq_band_high_hz"],
        window_margin_pre_ms=row["window_margin_pre_ms"],
        window_margin_post_ms=row["window_margin_post_ms"],
        min_corroborating_nodes=row["min_corroborating_nodes"],
        notes=row["notes"],
        updated_at=row["updated_at"],
    )


@router.get(
    "/species-tdoa-params",
    response_model=list[SpeciesTdoaParamsRecord],
    dependencies=[Depends(require_viewer)],
)
async def list_species_tdoa_params():
    """List all species TDOA params rows, including the __default__
    fallback sentinel."""
    rows = await db.list_species_tdoa_params()
    return [_species_tdoa_params_record_from_row(r) for r in rows]


@router.post(
    "/species-tdoa-params/reset-default",
    response_model=SpeciesTdoaParamsRecord,
    dependencies=[Depends(require_admin)],
)
async def reset_default_species_tdoa_params():
    """Recovery path: overwrite the __default__ row with hardcoded factory
    values, regardless of its current state. For when an operator has tuned
    __default__ into something broken and wants a known-good starting point
    back, without DB surgery. A static route (not /{species_key}/reset) since
    this only ever targets __default__ — every other species row simply has
    no row to "reset" to, only delete."""
    row = await db.reset_species_tdoa_params_to_factory_default()
    return _species_tdoa_params_record_from_row(row)


@router.get(
    "/species-tdoa-params/{species_key}",
    response_model=SpeciesTdoaParamsRecord,
    dependencies=[Depends(require_viewer)],
)
async def get_species_tdoa_params(species_key: str):
    """Return one species' TDOA params row. 404 if not configured — unlike
    node positions, there's no "empty default" response here, since an
    unconfigured species' effective params come from the __default__
    sentinel, which callers can fetch explicitly if they want to see it."""
    row = await db.get_species_tdoa_params(species_key)
    if row is None:
        raise HTTPException(status_code=404, detail="Species has no TDOA params row")
    return _species_tdoa_params_record_from_row(row)


@router.put(
    "/species-tdoa-params/{species_key}",
    response_model=SpeciesTdoaParamsRecord,
    dependencies=[Depends(require_admin)],
)
async def set_species_tdoa_params(species_key: str, req: SpeciesTdoaParams):
    """Create or update one species' TDOA params row (upsert, consistent
    with PUT /nodes/{id}/position).

    The __default__ sentinel can be updated like any other row (e.g. to
    retune the fallback margins) but not disabled — see the enabled check
    below.
    """
    if species_key == db.DEFAULT_SPECIES_KEY and not req.enabled:
        raise HTTPException(
            status_code=409,
            detail="The __default__ row cannot be disabled — it is the "
                   "fallback used when a species has no row of its own",
        )

    await db.upsert_species_tdoa_params(
        species_key,
        enabled=req.enabled,
        correlation_method=req.correlation_method,
        onset_detection_method=req.onset_detection_method,
        onset_threshold_factor=req.onset_threshold_factor,
        freq_band_low_hz=req.freq_band_low_hz,
        freq_band_high_hz=req.freq_band_high_hz,
        window_margin_pre_ms=req.window_margin_pre_ms,
        window_margin_post_ms=req.window_margin_post_ms,
        min_corroborating_nodes=req.min_corroborating_nodes,
        notes=req.notes,
        updated_at=_now_iso(),
    )
    log.info("species_tdoa_params upserted: %s", species_key)

    row = await db.get_species_tdoa_params(species_key)
    return _species_tdoa_params_record_from_row(row)


@router.delete(
    "/species-tdoa-params/{species_key}",
    status_code=204,
    dependencies=[Depends(require_admin)],
)
async def delete_species_tdoa_params(species_key: str):
    """Delete one species' TDOA params row.

    404 if it doesn't exist. 409 for __default__ — it must always exist as
    the orchestration pipeline's fallback (get_effective_species_tdoa_params
    raises if it's missing rather than silently degrading).
    """
    if species_key == db.DEFAULT_SPECIES_KEY:
        raise HTTPException(
            status_code=409,
            detail="The __default__ row cannot be deleted — it is the "
                   "fallback used when a species has no row of its own",
        )
    deleted = await db.delete_species_tdoa_params(species_key)
    if not deleted:
        raise HTTPException(status_code=404, detail="Species has no TDOA params row")
    log.info("species_tdoa_params deleted: %s", species_key)


# ---------------------------------------------------------------------------
# Detections
# ---------------------------------------------------------------------------

@router.get("/detections", response_model=list[DetectionRecord])
async def list_detections(
    limit: int = Query(default=200, ge=1, le=2000),
    min_conf: float = Query(default=0.0, ge=0.0, le=1.0),
    species: str | None = Query(default=None),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    time_of_day: str | None = Query(default=None, pattern="^(dawn|dusk|daytime|nighttime)$"),
):
    """Return the most recent BirdNET detections, newest first.

    species, min_conf, from, and to are optional filters — from/to are
    inclusive ISO8601 timestamp bounds on analyzed_at. time_of_day filters
    against the property's sun-relative dawn/dusk/daytime/nighttime windows
    (see suntimes.py) computed from the array_origin lat/lon — classification
    happens here in Python (after the SQL-level filters) rather than as a
    SQL range, since each row's window depends on its own calendar date.
    """
    # time_of_day classification is per-row, so over-fetch up to the API
    # ceiling when it's active rather than truncating to `limit` before
    # classifying — otherwise most of a `limit`-sized page could get
    # filtered out and we'd return far fewer rows than requested.
    fetch_limit = 2000 if time_of_day else limit
    rows = await db.list_detections(
        limit=fetch_limit, min_conf=min_conf, species=species, from_ts=from_ts, to_ts=to_ts,
    )

    if time_of_day:
        origin = await db.get_array_origin()
        if origin is None:
            raise HTTPException(
                status_code=400,
                detail="Array origin not set — configure the array's reference lat/lon before filtering by time of day",
            )
        buckets = suntimes.classify_many(
            (datetime.fromisoformat(r["analyzed_at"]) for r in rows), origin["lat"], origin["lon"],
        )
        rows = [r for r, bucket in zip(rows, buckets) if bucket == time_of_day][:limit]

    return [DetectionRecord(**row) for row in rows]


@router.post(
    "/species-links/resolve-missing",
    response_model=SpeciesLinksResolveResult,
    dependencies=[Depends(require_admin)],
)
async def resolve_missing_species_links():
    """Resolve Wikipedia/eBird links for every species in `detections` that
    has no species_links row yet, or whose row previously failed to
    resolve. Idempotent and safe to re-run — species already resolved
    ('ok' status) are left untouched.

    Backs the Settings tab's "Resolve missing species links" button. Also
    serves as the one-time backfill for species detected before this
    feature existed: species_links.maybe_resolve_new() (called after every
    detection insert going forward) only ever sees species as they're
    newly persisted, so anything already in `detections` before this table
    was added needs this sweep at least once.
    """
    return SpeciesLinksResolveResult(**await species_links.resolve_missing())


@router.get("/detections/species-summary", response_model=list[SpeciesSummary])
async def species_summary(
    min_conf: float = Query(default=0.0, ge=0.0, le=1.0),
    species: str | None = Query(default=None),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    time_of_day: str | None = Query(default=None, pattern="^(dawn|dusk|daytime|nighttime)$"),
):
    """Return per-species detection counts (count, last-seen, avg confidence),
    most-frequent-first — feeds the collapsible species list in the
    Detections tab.

    species/min_conf/from/to filter the same way as GET /detections.
    time_of_day is handled differently from the SQL GROUP BY path: since
    classification depends on each row's own calendar date, applying it
    requires fetching raw rows first and aggregating in Python. That fetch
    is capped at 2000 rows (the same ceiling GET /detections uses) — for a
    site with more than 2000 matching detections in the selected date range,
    per-species counts under a time_of_day filter could undercount. This is
    an acceptable v1 limitation; a more scalable version would compute each
    calendar day's sun windows and push them into the SQL WHERE clause as a
    union of ranges, avoiding the row cap entirely.
    """
    if not time_of_day:
        rows = await db.list_species_summary(
            min_conf=min_conf, species=species, from_ts=from_ts, to_ts=to_ts,
        )
        return [SpeciesSummary(**row) for row in rows]

    origin = await db.get_array_origin()
    if origin is None:
        raise HTTPException(
            status_code=400,
            detail="Array origin not set — configure the array's reference lat/lon before filtering by time of day",
        )

    raw = await db.list_detections(
        limit=2000, min_conf=min_conf, species=species, from_ts=from_ts, to_ts=to_ts,
    )
    buckets = suntimes.classify_many(
        (datetime.fromisoformat(r["analyzed_at"]) for r in raw), origin["lat"], origin["lon"],
    )
    matched = [r for r, bucket in zip(raw, buckets) if bucket == time_of_day]

    agg: dict[tuple[str, str], dict] = {}
    for r in matched:
        key = (r["common_name"], r["scientific_name"])
        entry = agg.setdefault(key, {
            "common_name": r["common_name"],
            "scientific_name": r["scientific_name"],
            "count": 0,
            "last_seen": r["analyzed_at"],
            "_conf_sum": 0.0,
        })
        entry["count"] += 1
        entry["_conf_sum"] += r["confidence"]
        if r["analyzed_at"] > entry["last_seen"]:
            entry["last_seen"] = r["analyzed_at"]

    # This path builds SpeciesSummary rows from raw detections in Python
    # rather than db.list_species_summary()'s SQL join, so wikipedia_url/
    # ebird_code have to be merged in separately here too — otherwise a
    # time_of_day-filtered view would silently lose the links every other
    # view of the species list has. links is small (one row per species ever
    # detected), cheap to fetch in full rather than filtering per-key.
    links = {row["species_key"]: row for row in await db.list_species_links()}

    summaries = [
        SpeciesSummary(
            common_name=v["common_name"],
            scientific_name=v["scientific_name"],
            count=v["count"],
            last_seen=v["last_seen"],
            avg_confidence=v["_conf_sum"] / v["count"],
            wikipedia_url=links.get(v["scientific_name"], {}).get("wikipedia_url"),
            ebird_code=links.get(v["scientific_name"], {}).get("ebird_code"),
        )
        for v in agg.values()
    ]
    summaries.sort(key=lambda s: s.count, reverse=True)
    return summaries


@router.post("/detections/analyze", response_model=list[DetectionRecord], status_code=201, dependencies=[Depends(require_admin)])
async def analyze_wav(file: UploadFile = File(...)):
    """Upload a WAV file, run BirdNET analysis, persist and return detections.

    The analyzer runs synchronously in a thread pool so the event loop is not
    blocked.  Returns 503 if the BirdNET model has not finished loading yet.
    """
    if not birdnet_worker.ready():
        raise HTTPException(status_code=503, detail="BirdNET model not yet loaded — try again shortly")

    import asyncio

    suffix = ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, birdnet_worker.analyze_wav, tmp_path)
    finally:
        os.unlink(tmp_path)

    if not raw:
        return []

    analyzed_at = datetime.now(timezone.utc).isoformat()
    source = file.filename or "upload"
    await db.insert_detections(source, analyzed_at, raw)
    species_links.maybe_resolve_new(raw)

    rows = await db.list_detections(limit=len(raw))
    return [DetectionRecord(**row) for row in rows[:len(raw)]]
