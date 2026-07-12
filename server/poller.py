"""Background task — periodically polls every known node's status endpoint.

Runs as an asyncio task for the lifetime of the app (see main.py lifespan).
Reachability + raw status land in the in-memory live-status cache in
`registry`, merged with persisted identity for the API response.
"""
import asyncio
import logging

import httpx

from . import config, db, registry, routes

log = logging.getLogger("sound_hub.poller")

# How often (seconds) to run db.prune_trigger_events. A day-granularity
# retention window doesn't need to be checked every STATUS_POLL_INTERVAL_S
# (5s) tick — that would just be unnecessary DELETE churn against a large
# table. Once an hour is frequent enough that steady-state prunes stay small
# (only the rows that cross the retention boundary since the last run).
TRIGGER_EVENTS_PRUNE_INTERVAL_S = 3600.0

# How often (seconds) to run db.rollup_trigger_events. Every 5 minutes keeps
# trigger_event_rollups reasonably current without adding meaningful load —
# each run only aggregates the last interval's worth of new rows per node
# (see rollup_trigger_events()'s per-node watermark), not the whole table.
TRIGGER_EVENT_ROLLUP_INTERVAL_S = 300.0


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
        raw_status = resp.json()
        registry.update_live_status(node_id, reachable=True, raw_status=raw_status)
        # Feed the hub-side GPS EMA (replaces firmware's removed GpsCentroid —
        # see GPS-TELEMETRY-SIMPLIFICATION-PROPOSAL.md). Deliberately only
        # called on a successful poll: a failed poll tells us nothing about
        # whether the node's GPS is still locked, so the EMA/settle-timer
        # state is just left as-is rather than treated as a lock loss.
        registry.update_gps_ema(node_id, raw_status)
        # First successful poll of a node that hasn't been given the hub's
        # address yet (covers add_manual_node's pre-provisioned/unreachable-
        # at-add-time path — see routes.push_hub_address_to_node). Reuses
        # this poll's client rather than opening a new connection.
        if not node["configured"]:
            if await routes.push_hub_address_to_node(ip_address, client=client):
                await registry.set_configured(node_id, True)
    except (httpx.HTTPError, ValueError) as exc:
        # Distinguish failure classes in the log — previously every cause
        # (timeout, connection refused, TLS error, non-2xx status, bad JSON)
        # was folded into the same debug-only line, which made it impossible
        # to tell why a node got marked unreachable after the fact.
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f"HTTP {exc.response.status_code}"
        elif isinstance(exc, httpx.TimeoutException):
            detail = "timeout"
        elif isinstance(exc, httpx.ConnectError):
            detail = "connection error"
        else:
            detail = type(exc).__name__
        log.warning("Poll failed for %s @ %s: %s (%s)", node_id, ip_address, detail, exc)
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
    loop = asyncio.get_event_loop()
    next_rollup_at = 0.0  # 0 forces a rollup check on the very first iteration
    next_prune_at = 0.0   # 0 forces a prune check on the very first iteration
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

                now = loop.time()

                # Rollup runs before prune so raw rows are always summarized
                # into trigger_event_rollups well before they age out — the
                # rollup's 2-minute safety margin is trivially shorter than
                # prune's 1-day retention, so ordering here is a formality,
                # not a correctness requirement, but keeping it explicit
                # avoids ever having to reason about it again.
                if now >= next_rollup_at:
                    rolled = await db.rollup_trigger_events()
                    if rolled:
                        log.debug("trigger_events: rolled up %d bucket(s)", rolled)
                    next_rollup_at = now + TRIGGER_EVENT_ROLLUP_INTERVAL_S

                if now >= next_prune_at:
                    deleted = await db.prune_trigger_events()
                    if deleted:
                        log.info("trigger_events: pruned %d rows older than %dh",
                                  deleted, db.TRIGGER_EVENTS_RETENTION_HOURS)
                    next_prune_at = now + TRIGGER_EVENTS_PRUNE_INTERVAL_S
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Poller iteration failed — continuing")
            await asyncio.sleep(config.STATUS_POLL_INTERVAL_S)
