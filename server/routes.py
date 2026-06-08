"""API routes consumed by the React frontend (mounted under /api in main.py)."""
import logging

import httpx
from fastapi import APIRouter, HTTPException

from . import config, db, registry, status_mapper
from .models import ManualNodeRequest, NodeConfigRequest, NodeView

log = logging.getLogger("sound_hub.routes")
router = APIRouter()


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
        gps=derived["gps"],
        clock=derived["clock"],
        audio=derived["audio"],
        esp_now=derived["esp_now"],
        flags=derived["flags"],
    )


async def _mapped_nodes() -> list[tuple[dict, dict, dict]]:
    """Map every registered node's raw status, then run the cross-node
    derivation pass (projecting leaf nodes' relative offsets onto the
    primary's absolute position — see status_mapper.derive_relative_positions).

    This has to happen across the whole set at once: deriving a leaf node's
    lat/lon requires knowing the primary's surveyed/estimated position, which
    isn't available when mapping a single node in isolation.
    """
    nodes = await registry.list_nodes()
    triples = []
    for node in nodes:
        live = registry.get_live_status(node["id"])
        derived = status_mapper.map_status(
            role=node["role"],
            reachable=live["reachable"],
            raw_status=live["raw_status"],
        )
        triples.append((node, live, derived))

    status_mapper.derive_relative_positions([derived for _, _, derived in triples])
    return triples


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/nodes", response_model=list[NodeView])
async def list_nodes():
    return [_build_view(node, live, derived) for node, live, derived in await _mapped_nodes()]


@router.get("/nodes/{node_id}", response_model=NodeView)
async def get_node(node_id: str):
    for node, live, derived in await _mapped_nodes():
        if node["id"] == node_id:
            return _build_view(node, live, derived)
    raise HTTPException(status_code=404, detail="Node not found")


@router.post("/nodes/manual", response_model=NodeView)
async def add_manual_node(req: ManualNodeRequest):
    """Fallback discovery path — add a node by hostname or bare IP.

    Validated by hitting its status endpoint directly; if that responds
    with something sane, we register it the same way an mDNS discovery would.
    """
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

    # Confirmed against a live node: status_json["node"]["hostname"] is the
    # canonical id (e.g. "soundcapture-ed5de4"). Fall back to the supplied
    # host string only if that's somehow missing.
    node_id = (status_json.get("node") or {}).get("hostname") or host

    await registry.upsert_node(node_id=node_id, hostname=node_id, ip_address=host,
                               discovery_method="manual")
    registry.update_live_status(node_id, reachable=True, raw_status=status_json)

    for n, live, derived in await _mapped_nodes():
        if n["id"] == node_id:
            return _build_view(n, live, derived)
    raise HTTPException(status_code=500, detail="Node registered but not found in mapped set")


@router.delete("/nodes/{node_id}", status_code=204)
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


@router.post("/nodes/{node_id}/approve", response_model=NodeView)
async def approve_node(node_id: str):
    """Admit a discovered node into the active array.

    Moves a node to `approved` — from here it's polled, shown on the map,
    and included in position derivation. Works from any prior state
    (pending → approved is the normal path; rejected → approved is a
    deliberate reversal — see `reject_node`).
    """
    return await _set_approval(node_id, db.APPROVED)


@router.post("/nodes/{node_id}/reject", response_model=NodeView)
async def reject_node(node_id: str):
    """Decline a discovered node — keep it out of the active array.

    Deliberately does NOT delete the node: rejection is a reversible
    operator decision (e.g. "not yet", "wrong network", "investigate
    first"), and re-discovering a deleted node from scratch loses any
    context about why it was declined. Use DELETE /nodes/{id} if you're
    sure you never want to see this node again.
    """
    return await _set_approval(node_id, db.REJECTED)


@router.get("/nodes/{node_id}/config")
async def get_node_config(node_id: str):
    """Fetch a node's current persisted config — used to pre-fill the
    operator's edit form so they're editing real values, not guessing from
    the (possibly stale/derived) status view."""
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


@router.post("/nodes/{node_id}/configure")
async def configure_node(node_id: str, req: NodeConfigRequest):
    """Push config changes to a node by proxying to its own
    POST /app/api/node-config (NodeConfigHandler.cpp — persists to NVS).

    Only fields the operator actually changed are included in `req`
    (`exclude_unset`), so we forward exactly that subset rather than
    clobbering fields the form didn't touch.

    Origin guard: at most one node in the array should ever be flagged
    `isOrigin` — it's the tangent point every other node's relative
    position is resolved against (see status_mapper._offset_to_latlon).
    Two origins would silently produce ambiguous lat/lon. The firmware
    has no cross-node visibility to enforce this itself, so the hub —
    the only thing that sees every node — is responsible. We block a
    push that would create a second origin; the operator must clear the
    existing one first (a deliberate two-step "move the origin" flow,
    not silently auto-clearing it for them).
    """
    node = await registry.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")

    body = req.model_dump(by_alias=True, exclude_unset=True, exclude_none=True)

    if body.get("isOrigin") is True:
        for other, other_live, _derived in await _mapped_nodes():
            if other["id"] == node_id:
                continue
            other_cfg = (other_live.get("raw_status") or {}).get("node") or {}
            if other_cfg.get("isOrigin"):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"'{other['hostname']}' is already set as the origin. "
                        "Clear it there first, then set this node as origin."
                    ),
                )

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


@router.post("/nodes/{node_id}/sample")
async def request_sample(node_id: str):
    """Request an audio sample pull from a node. STUB — pull protocol TBD."""
    node = await registry.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    raise HTTPException(
        status_code=501,
        detail="Not implemented — awaiting audio pull protocol",
    )
