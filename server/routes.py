"""API routes consumed by the React frontend (mounted under /api in main.py)."""
import logging

import httpx
from fastapi import APIRouter, HTTPException

from . import config, registry, status_mapper
from .models import ManualNodeRequest, NodeView

log = logging.getLogger("acoustic_base.routes")
router = APIRouter()


def _build_view(node: dict, live: dict, derived: dict) -> NodeView:
    return NodeView(
        id=node["id"],
        hostname=node["hostname"],
        ip_address=node["ip_address"],
        role=derived["role"],
        discovery_method=node["discovery_method"],
        configured=bool(node["configured"]),
        reachable=live["reachable"],
        last_seen_at=live["last_seen_at"],
        raw_status=live["raw_status"],
        status=derived["status"],
        lat_lon=derived["lat_lon"],
        position_relative=derived["position_relative"],
        position_known=derived["position_known"],
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


@router.post("/nodes/{node_id}/configure")
async def configure_node(node_id: str):
    """Push base-station config to a node (address, push endpoint, role, ...).

    STUB. Per the provisioning design in memory: the base pushes config to
    newly-discovered nodes rather than nodes needing pre-baked addresses.
    Awaiting a node-side config endpoint before this can do anything real.
    """
    node = await registry.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    raise HTTPException(
        status_code=501,
        detail="Not implemented — awaiting node-side provisioning endpoint",
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
