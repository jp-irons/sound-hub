"""Background task — periodically polls every known node's status endpoint.

Runs as an asyncio task for the lifetime of the app (see main.py lifespan).
Reachability + raw status land in the in-memory live-status cache in
`registry`, merged with persisted identity for the API response.
"""
import asyncio
import logging

import httpx

from . import config, db, registry

log = logging.getLogger("sound_hub.poller")


async def _poll_one(client: httpx.AsyncClient, node: dict) -> None:
    node_id = node["id"]
    ip_address = node["ip_address"]

    if not ip_address:
        registry.update_live_status(node_id, reachable=False, raw_status=None)
        return

    # Nodes serve over HTTPS with (presumably) a self-signed cert.
    url = f"{config.NODE_SCHEME}://{ip_address}/app/api/status"
    try:
        resp = await client.get(url, timeout=config.STATUS_TIMEOUT_S)
        resp.raise_for_status()
        registry.update_live_status(node_id, reachable=True, raw_status=resp.json())
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("Poll failed for %s @ %s: %s", node_id, ip_address, exc)
        registry.update_live_status(node_id, reachable=False, raw_status=None)


async def run() -> None:
    log.info("Status poller started — interval %.1fs", config.STATUS_POLL_INTERVAL_S)
    async with httpx.AsyncClient(verify=False) as client:
        while True:
            try:
                # Only poll approved nodes — pending/rejected nodes haven't
                # been admitted to the active array yet (or have been
                # explicitly declined), so we don't reach out to them. This
                # also means they never accrue gps/audio/clock data, which
                # keeps them harmlessly absent from the map and TDOA-relevant
                # views without any extra filtering downstream.
                nodes = [n for n in await registry.list_nodes()
                         if n["approval_status"] == db.APPROVED]
                if nodes:
                    await asyncio.gather(*(_poll_one(client, n) for n in nodes))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Poller iteration failed — continuing")
            await asyncio.sleep(config.STATUS_POLL_INTERVAL_S)
