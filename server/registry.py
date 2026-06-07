"""Node registry — persisted identity (SQLite) + in-memory live status cache.

Identity records (hostname, IP, how we found it, whether it's been
provisioned) survive restarts. Live status is re-populated by the poller
within one polling interval of startup, so keeping it in-memory only
keeps this simple and avoids a write-heavy DB.
"""
import asyncio
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from . import db

_write_lock = asyncio.Lock()
_live_status: dict[str, dict] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def upsert_node(node_id: str, hostname: str, ip_address: Optional[str],
                       discovery_method: str = "mdns") -> None:
    """Insert a newly-seen node, or refresh its hostname/IP if already known."""
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


async def set_configured(node_id: str, configured: bool = True) -> None:
    async with _write_lock:
        async with db.connect() as conn:
            await conn.execute("UPDATE nodes SET configured = ? WHERE id = ?",
                               (1 if configured else 0, node_id))
            await conn.commit()


# --- Live status (in-memory, volatile) ---

def update_live_status(node_id: str, reachable: bool, raw_status: Optional[dict]) -> None:
    prev = _live_status.get(node_id, {})
    _live_status[node_id] = {
        "reachable": reachable,
        "last_seen_at": _now_iso() if reachable else prev.get("last_seen_at"),
        "raw_status": raw_status if raw_status is not None else prev.get("raw_status"),
    }


def get_live_status(node_id: str) -> dict:
    return _live_status.get(node_id, {"reachable": False, "last_seen_at": None, "raw_status": None})
