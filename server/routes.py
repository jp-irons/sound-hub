"""API routes consumed by the React frontend (mounted under /api in main.py)."""
import asyncio
import logging
import os
import tempfile
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from . import birdnet_worker, config, db, registry, status_mapper
from .auth import get_current_user, require_admin, require_node, require_viewer
from .models import (
    ArrayOrigin, ArrayOriginManual,
    AudioAckBody, AudioSampleRequest, DetectionRecord, ManualNodeRequest,
    NodeConfigRequest, NodePosition, NodeView, TdoaRequest, TdoaResponse,
    LatLon,
)
from .tdoa_solver import Node as TdoaNode, solve as tdoa_solve

# Directory for saved audio files (created on first push).
_AUDIO_DIR = os.path.join(os.path.dirname(__file__), "..", "audio")

log = logging.getLogger("sound_hub.routes")
router = APIRouter()


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
        surveyed_lat=pos.get("surveyed_lat"),
        surveyed_lon=pos.get("surveyed_lon"),
        surveyed_alt=pos.get("surveyed_alt"),
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
    """Fallback discovery path — add a node by hostname or bare IP."""
    host = req.host.strip()
    if not host:
        raise HTTPException(status_code=400, detail="host must not be empty")

    url = f"{config.NODE_SCHEME}://{host}/app/api/status"
    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(url, timeout=config.STATUS_TIMEOUT_S)
            resp.raise_for_status()
            status_json = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not reach a node status API at '{host}': {exc}",
        )

    node_id = (status_json.get("node") or {}).get("hostname") or host

    await registry.upsert_node(node_id=node_id, hostname=node_id, ip_address=host,
                               discovery_method="manual")
    registry.update_live_status(node_id, reachable=True, raw_status=status_json)

    for n, live, derived in await _mapped_nodes():
        if n["id"] == node_id:
            return _build_view(n, live, derived)
    raise HTTPException(status_code=500, detail="Node registered but not found in mapped set")


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
    """Set or update the hub-stored position for a node.

    The unique partial index on is_origin=1 enforces the one-origin
    invariant at the DB level — a second node trying to claim is_origin=True
    will get an IntegrityError surfaced as a 409.
    """
    node = await registry.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")

    # is_origin on node_positions is now an informational marker only — set
    # automatically when POST /api/origin/set-from-node is called.  We still
    # accept it here for backward compat but do not enforce uniqueness or
    # call clear_origin(); that invariant is managed by set_array_origin().
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
        surveyed_lat=req.surveyed_lat,
        surveyed_lon=req.surveyed_lon,
        surveyed_alt=req.surveyed_alt,
        updated_at=_now_iso(),
    )

    pos = await db.get_node_position(node_id)
    return _node_position_from_row(pos)


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
async def set_origin_from_node(
    node_id: str,
    source: str = Query(
        default="gps_centroid",
        description="Coordinate source: 'gps_centroid' (live GPS average) or 'surveyed_coords' (stored lat/lon/alt)",
    ),
):
    """Compute and store the hub array origin from a surveyed node's position.

    Back-projects the array (0,0,0) datum from the node's reference coordinates
    minus its stored N/E/Alt array offset:
        origin = ref_latlon - node_array_offset

    source=gps_centroid  — use the node's live GPS centroid (default).
    source=surveyed_coords — use the operator-entered lat/lon/alt stored for
                            this node (surveyedLat/Lon/Alt in node_positions).

    Either way the math is identical — the node's N/E/Alt offset is preserved
    so all other surveyed nodes remain valid with no re-surveying required.
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

    if source == "surveyed_coords":
        # Use the stored surveyed coordinates.
        ref_lat = node_pos.get("surveyed_lat")
        ref_lon = node_pos.get("surveyed_lon")
        ref_alt = node_pos.get("surveyed_alt")
        if ref_lat is None or ref_lon is None or ref_alt is None:
            raise HTTPException(
                status_code=422,
                detail="No surveyed coordinates stored for this node — enter lat/lon/alt first",
            )
    else:
        # Default: use the live GPS centroid.
        live = registry.get_live_status(node_id)
        raw = live.get("raw_status") or {}
        centroid = raw.get("gpsCentroid") or {}
        if centroid.get("latitude") is None or centroid.get("longitude") is None:
            raise HTTPException(
                status_code=422,
                detail="Node has no GPS centroid yet — wait for GPS to converge",
            )
        ref_lat = centroid["latitude"]
        ref_lon = centroid["longitude"]
        ref_alt = centroid.get("altitudeM", 0.0)

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
    return ArrayOrigin(lat=req.lat, lon=req.lon, alt_m=req.alt_m, set_from=None, set_at=set_at)


@router.delete("/origin", status_code=204, dependencies=[Depends(require_admin)])
async def clear_origin():
    """Clear the hub array origin and the is_origin marker on all nodes.

    After this call all node lat/lon projections become unavailable until a
    new origin is set.
    """
    await db.clear_array_origin()


# ---------------------------------------------------------------------------
# Audio pull — control plane endpoints
# ---------------------------------------------------------------------------

@router.post("/audio/ack", status_code=204, dependencies=[Depends(require_node)])
async def audio_ack(body: AudioAckBody):
    """Receive a broker-forwarded node ack (ACK / DONE / UNAVAILABLE / ERROR).

    The broker calls this after hearing an AudioAckMsg from a node over ESP-NOW.
    Updates the in-memory request tracker so callers polling
    GET /audio/requests/{id} see the latest status.
    """
    entry = _audio_requests.setdefault(body.request_id, {"acks": []})
    entry["acks"].append({
        "status": body.status,
        "srcMac": body.src_mac,
        "at": _now_iso(),
    })
    log.info("audio ack id=%s status=%s from %s", body.request_id, body.status, body.src_mac)


@router.post("/audio/push", status_code=200, dependencies=[Depends(require_node)])
async def audio_push(
    request: Request,
    requestId: int = Query(..., description="Matches the originating AudioRequestMsg.requestId"),
    srcMac: str    = Query(..., description="Wi-Fi STA MAC of the sending node (xx:xx:xx:xx:xx:xx)"),
):
    """Receive a WAV audio segment pushed directly from a node.

    The node POSTs here after receiving an AUDIO_REQUEST and successfully
    retrieving the segment from its ring buffer.  The file is saved to the
    audio/ directory as audio_{requestId}_{srcMac}.wav.
    """
    data = await request.body()
    os.makedirs(_AUDIO_DIR, exist_ok=True)
    fname = f"audio_{requestId}_{srcMac.replace(':', '')}.wav"
    with open(os.path.join(_AUDIO_DIR, fname), "wb") as fh:
        fh.write(data)
    entry = _audio_requests.setdefault(requestId, {"acks": []})
    entry.update({"file": fname, "bytes": len(data), "savedAt": _now_iso()})
    log.info("audio push id=%s from %s — %d bytes → %s", requestId, srcMac, len(data), fname)


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


@router.post("/nodes/{node_id}/sample", status_code=202, dependencies=[Depends(require_admin)])
async def request_sample(node_id: str, req: AudioSampleRequest):
    """Trigger an audio pull from a node for a specific UTC time range.

    Flow:
      1. Resolves the target node's wifiMac from its last polled status.
      2. Finds the currently reachable broker node.
      3. POSTs the AudioRequestMsg JSON to the broker's /espnow/relay endpoint.
      4. Returns {requestId, status: "relayed"} immediately — poll
         GET /audio/requests/{requestId} for ack and push progress.

    Errors:
      404 — node not found in registry
      422 — node wifiMac not yet known (wait for next status poll)
      503 — no reachable broker, or broker IP unknown
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

    import random
    request_id = random.randint(1, 2**31 - 1)
    _audio_requests[request_id] = {
        "acks": [],
        "targetMac": target_mac,
        "createdAt": _now_iso(),
    }

    payload = {
        "requestId": request_id,
        "targetMac": target_mac,
        "tStartUs":  req.t_start_us,
        "tEndUs":    req.t_end_us,
        "hubIp":     config.BASE_STATION_IP,
        "hubPort":   config.BASE_STATION_PORT,
    }

    async with httpx.AsyncClient(verify=False, timeout=5.0) as client:
        r = await client.post(f"{config.NODE_SCHEME}://{broker_ip}/espnow/relay", json=payload)

    if r.status_code not in (200, 202):
        raise HTTPException(
            status_code=502,
            detail=f"Broker relay rejected: HTTP {r.status_code}",
        )

    log.info("audio pull relayed -- id=%s node=%s mac=%s [%s ... %s]",
             request_id, node_id, target_mac, req.t_start_us, req.t_end_us)
    return {"requestId": request_id, "status": "relayed"}


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


# ---------------------------------------------------------------------------
# Detections
# ---------------------------------------------------------------------------

@router.get("/detections", response_model=list[DetectionRecord])
async def list_detections(limit: int = Query(default=200, ge=1, le=2000)):
    """Return the most recent BirdNET detections, newest first."""
    rows = await db.list_detections(limit=limit)
    return [DetectionRecord(**row) for row in rows]


@router.post("/detections/analyze", response_model=list[DetectionRecord], status_code=201)
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

    rows = await db.list_detections(limit=len(raw))
    return [DetectionRecord(**row) for row in rows[:len(raw)]]
