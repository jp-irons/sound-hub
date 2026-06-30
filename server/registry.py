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

from . import db

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
                       discovery_method: str = "mdns") -> None:
    """Insert a newly-seen node, or refresh its hostname/IP if already known.

    New nodes land with `approval_status='pending'` (the column default) —
    being seen on the network is not the same as being trusted. The
    ON CONFLICT branch deliberately does NOT touch `approval_status`, so an
    operator's approve/reject decision survives repeated mDNS re-discovery
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
