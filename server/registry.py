"""Node registry — persisted identity (SQLite) + in-memory live status cache.

Identity records (hostname, IP, how we found it, whether it's been
provisioned) survive restarts. Live status is re-populated by the poller
within one polling interval of startup, so keeping it in-memory only
keeps this simple and avoids a write-heavy DB.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from . import config, db

log = logging.getLogger("sound_hub.registry")

_write_lock = asyncio.Lock()
_live_status: dict[str, dict] = {}

# Consecutive poll-only failures required before a previously-reachable node
# is actually flipped to "offline". Added 2026-06-30 after node 170 showed
# offline overnight while its self-registration POSTs (a separate, plain-HTTP
# reachability signal — see register_node() in routes.py) succeeded the
# entire time: the periodic HTTPS poller flipped `reachable` on a single
# failed poll with no debounce, racing/clobbering the register-confirmed
# state. A successful call into update_live_status(reachable=True) — from
# either the poller or registration — resets this counter immediately, so a
# genuinely offline node (no register calls arriving either) still gets
# marked offline after this many consecutive poll failures.
_OFFLINE_FAILURE_THRESHOLD = 3

_consec_failures: dict[str, int] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def upsert_node(node_id: str, hostname: str, ip_address: Optional[str],
                       discovery_method: str) -> None:
    """Insert a newly-seen node, or refresh its hostname/IP if already known.

    New nodes land with `approval_status='pending'` (the column default) —
    being seen on the network is not the same as being trusted. The
    ON CONFLICT branch deliberately does NOT touch `approval_status`, so an
    operator's approve/reject decision survives repeated re-registration
    of the same node (e.g. after a reboot or Wi-Fi blip).
    """
    async with _write_lock:
        async with db.connect() as conn:
            await conn.execute(
                """INSERT INTO nodes (id, hostname, ip_address, discovery_method, discovered_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       hostname=excluded.hostname,
                       ip_address=excluded.ip_address""",
                (node_id, hostname, ip_address, discovery_method, _now_iso()),
            )
            await conn.commit()


async def set_approval_status(node_id: str, status: str) -> None:
    """Set a node's approval status — one of db.PENDING/APPROVED/REJECTED.

    This is the single place an operator's admit/reject/re-approve decision
    is persisted. Rejected nodes are not deleted (see db.REJECTED docstring)
    so the decision can be reversed without re-discovering the node.
    """
    async with _write_lock:
        async with db.connect() as conn:
            await conn.execute("UPDATE nodes SET approval_status = ? WHERE id = ?",
                               (status, node_id))
            await conn.commit()


async def list_nodes() -> list[dict]:
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM nodes ORDER BY hostname")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_node(node_id: str) -> Optional[dict]:
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def remove_node(node_id: str) -> None:
    async with _write_lock:
        async with db.connect() as conn:
            await conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            await conn.commit()
    _live_status.pop(node_id, None)
    _consec_failures.pop(node_id, None)


async def set_configured(node_id: str, configured: bool = True) -> None:
    async with _write_lock:
        async with db.connect() as conn:
            await conn.execute("UPDATE nodes SET configured = ? WHERE id = ?",
                               (1 if configured else 0, node_id))
            await conn.commit()


async def update_node_ip(node_id: str, ip_address: str) -> None:
    """Refresh a known node's IP from an inbound heartbeat's source address
    (see routes.node_heartbeat) — a lightweight defence against a node's
    DHCP lease changing without an operator re-adding it.

    UPDATE only, deliberately not upsert_node: this never creates a row
    (heartbeat 404s for an unknown node_id before this is ever called) and
    doesn't touch identity/approval_status/discovery_method, unlike
    upsert_node's ON CONFLICT branch.
    """
    async with _write_lock:
        async with db.connect() as conn:
            await conn.execute("UPDATE nodes SET ip_address = ? WHERE id = ?",
                               (ip_address, node_id))
            await conn.commit()


# --- Live status (in-memory, volatile) ---

def update_live_status(node_id: str, reachable: bool, raw_status: Optional[dict]) -> None:
    prev = _live_status.get(node_id, {})

    if reachable:
        _consec_failures[node_id] = 0
        effective_reachable = True
    else:
        failures = _consec_failures.get(node_id, 0) + 1
        _consec_failures[node_id] = failures
        was_reachable = prev.get("reachable", False)
        if was_reachable and failures < _OFFLINE_FAILURE_THRESHOLD:
            log.info(
                "Node %s: poll failure %d/%d while previously reachable — "
                "holding 'online' (debounced)",
                node_id, failures, _OFFLINE_FAILURE_THRESHOLD,
            )
            effective_reachable = True
        else:
            effective_reachable = False

    _live_status[node_id] = {
        "reachable": effective_reachable,
        "last_seen_at": _now_iso() if effective_reachable else prev.get("last_seen_at"),
        "raw_status": raw_status if raw_status is not None else prev.get("raw_status"),
        "reg_heap_free_bytes": prev.get("reg_heap_free_bytes"),
        "reg_heap_min_free_bytes": prev.get("reg_heap_min_free_bytes"),
        "reg_heap_at": prev.get("reg_heap_at"),
        "reg_https_active_sockets": prev.get("reg_https_active_sockets"),
        "reg_https_max_sockets": prev.get("reg_https_max_sockets"),
    }


def update_registration_heap(node_id: str, heap_free_bytes: Optional[int],
                              heap_min_free_bytes: Optional[int]) -> None:
    """Record heap telemetry sent with a node's self-registration POST.

    This arrives over plain HTTP alongside /api/nodes/register, independent
    of whether the node's HTTPS status endpoint is currently reachable — so
    it keeps reporting even while the node is in the "HTTPS resets, HTTP
    still works" degraded state under investigation.
    """
    if heap_free_bytes is None and heap_min_free_bytes is None:
        return
    prev = _live_status.get(node_id, {
        "reachable": False, "last_seen_at": None, "raw_status": None,
    })
    prev["reg_heap_free_bytes"] = heap_free_bytes
    prev["reg_heap_min_free_bytes"] = heap_min_free_bytes
    prev["reg_heap_at"] = _now_iso()
    _live_status[node_id] = prev


def update_registration_sockets(node_id: str, active_sockets: Optional[int],
                                 max_sockets: Optional[int]) -> None:
    """Record HTTPS socket-pool telemetry sent with a node's self-registration
    POST — same rationale as update_registration_heap(): arrives over plain
    HTTP, independent of whether the HTTPS status endpoint is reachable.

    Used to test the theory that the .150-style TLS-handshake resets are
    caused by the httpd_ssl_start connection-slot pool filling up with
    stuck/half-open sockets rather than heap exhaustion.
    """
    if active_sockets is None and max_sockets is None:
        return
    prev = _live_status.get(node_id, {
        "reachable": False, "last_seen_at": None, "raw_status": None,
    })
    prev["reg_https_active_sockets"] = active_sockets
    prev["reg_https_max_sockets"] = max_sockets
    _live_status[node_id] = prev


def get_live_status(node_id: str) -> dict:
    return _live_status.get(node_id, {
        "reachable": False, "last_seen_at": None, "raw_status": None,
        "reg_heap_free_bytes": None, "reg_heap_min_free_bytes": None, "reg_heap_at": None,
        "reg_https_active_sockets": None, "reg_https_max_sockets": None,
    })


# --- GPS EMA (in-memory, volatile) ---
#
# Hub-side replacement for the firmware's removed GpsCentroid (Welford +
# EMA) — see GPS-TELEMETRY-SIMPLIFICATION-PROPOSAL.md in sound-capture-node.
# One continuously-updated, half-life-decayed lat/lon/alt average per node,
# fed from the raw GPS fix on every poll. Never persisted, same rationale as
# `_live_status`: a hub restart just reconverges within a couple of
# half-lives (a few minutes), so there's nothing worth writing to disk.
#
# Quality gates mirror the firmware's old GpsCentroid thresholds exactly, so
# behavior doesn't silently change: a fix is only admitted once satellites
# >= _GPS_EMA_MIN_SATELLITES, and only after the node's GPS has held lock
# continuously for at least _GPS_EMA_LOCK_SETTLE_S (settles the receiver's
# solution past its initial coarse fix, same as the firmware's
# kLockSettleS). Losing lock resets the settle timer, same as the firmware
# resetting lockAcquiredAt_ on loss of fix.
_gps_ema: dict[str, dict] = {}

_GPS_EMA_MIN_SATELLITES = 8    # matches firmware's GpsCentroid::kMinSatellitesForSample
_GPS_EMA_LOCK_SETTLE_S  = 30.0 # matches firmware's GpsCentroid::kLockSettleS
_GPS_EMA_HALF_LIFE_S    = 600.0  # 10 minutes

# Decay per poll, derived from the half-life and the *configured* poll
# interval (not a hardcoded guess) so the effective half-life stays correct
# even if an operator changes STATUS_POLL_INTERVAL_S.
_GPS_EMA_DECAY = 0.5 ** (config.STATUS_POLL_INTERVAL_S / _GPS_EMA_HALF_LIFE_S)


def update_gps_ema(node_id: str, raw_status: Optional[dict]) -> None:
    """Feed one poll's raw GPS fix into a node's hub-side EMA.

    Call this from the poller on every status fetch (successful or not —
    pass raw_status=None on failure, which is treated the same as no fix:
    lock-settle timer resets, no sample admitted).
    """
    gps = (raw_status or {}).get("gps") or {}
    now = datetime.now(timezone.utc).timestamp()
    state = _gps_ema.get(node_id)

    if not gps.get("available") or not gps.get("receiving"):
        # No current lock — require the solution to settle again on
        # reacquisition, same as the firmware's lockAcquiredAt_ reset.
        if state is not None:
            state["lock_since"] = None
        return

    if state is None:
        state = {"lat": None, "lon": None, "alt": None, "n": 0, "lock_since": None}
        _gps_ema[node_id] = state

    if state["lock_since"] is None:
        state["lock_since"] = now
    if now - state["lock_since"] < _GPS_EMA_LOCK_SETTLE_S:
        return
    if (gps.get("satellites") or 0) < _GPS_EMA_MIN_SATELLITES:
        return

    lat, lon = gps.get("latitude"), gps.get("longitude")
    if lat is None or lon is None:
        return
    alt = gps.get("altitudeM")
    alt = alt if alt is not None else 0.0

    if state["n"] == 0:
        # First admitted sample — seed directly rather than decaying from
        # None (mirrors the firmware EMA's cold-start behavior).
        state["lat"], state["lon"], state["alt"] = lat, lon, alt
    else:
        d = _GPS_EMA_DECAY
        state["lat"] = d * state["lat"] + (1 - d) * lat
        state["lon"] = d * state["lon"] + (1 - d) * lon
        state["alt"] = d * state["alt"] + (1 - d) * alt
    state["n"] += 1


def get_gps_ema(node_id: str) -> Optional[dict]:
    """Return {"lat", "lon", "alt", "n"} for a node's live GPS EMA, or None
    if no sample has been admitted yet (no lock, or still settling)."""
    state = _gps_ema.get(node_id)
    if state is None or state["n"] == 0:
        return None
    return {"lat": state["lat"], "lon": state["lon"], "alt": state["alt"], "n": state["n"]}
