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


def _parse_trigger_diag_csv(text: str) -> list[dict]:
    """Parse the CSV body of GET /app/api/trigger-diag into row dicts.

    Format (see TriggerDiagHandler.cpp on the node side):
        timeUs,energyRatio,fluxRatio,fired
        <int64>,<float>,<float>,<0|1>
        ...
    Malformed lines are skipped rather than aborting the whole poll — a
    single corrupt row shouldn't lose the rest of the batch.
    """
    rows: list[dict] = []
    lines = text.strip().splitlines()
    for line in lines[1:]:  # skip header row
        parts = line.split(",")
        if len(parts) != 4:
            continue
        try:
            rows.append({
                "t_us": int(parts[0]),
                "energy_ratio": float(parts[1]),
                "flux_ratio": float(parts[2]),
                "fired": parts[3].strip() == "1",
            })
        except ValueError:
            continue
    return rows


async def _poll_trigger_diag(client: httpx.AsyncClient, node: dict) -> None:
    """Pull a node's trigger-diagnostics ring buffer and persist new rows.

    Separate from _poll_one (status) so a parse/DB failure here never
    affects status reachability tracking. Skipped entirely for nodes with
    no IP (same as _poll_one) since there's nothing to reach.
    """
    node_id = node["id"]
    ip_address = node["ip_address"]
    if not ip_address:
        return

    url = f"{config.NODE_SCHEME}://{ip_address}/app/api/trigger-diag"
    try:
        resp = await client.get(url, timeout=config.STATUS_TIMEOUT_S)
        resp.raise_for_status()
        rows = _parse_trigger_diag_csv(resp.text)
        if rows:
            inserted = await db.insert_trigger_events(node_id, rows)
            if inserted:
                log.debug("trigger-diag: %s — %d new rows (of %d seen)",
                          node_id, inserted, len(rows))
    except httpx.HTTPError as exc:
        log.debug("trigger-diag poll failed for %s @ %s: %s", node_id, ip_address, exc)


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
                    await asyncio.gather(
                        *(_poll_one(client, n) for n in nodes),
                        *(_poll_trigger_diag(client, n) for n in nodes),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Poller iteration failed — continuing")
            await asyncio.sleep(config.STATUS_POLL_INTERVAL_S)
