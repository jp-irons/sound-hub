"""SQLite persistence for the node registry.

Only identity/provisioning state is persisted here (a node, once known,
should survive a backend restart). Live status from polling is kept
in-memory in `registry` — it's volatile and re-populated within seconds
of startup.
"""
import aiosqlite

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id               TEXT PRIMARY KEY,
    hostname         TEXT NOT NULL,
    ip_address       TEXT,
    role             TEXT DEFAULT 'UNKNOWN',
    discovery_method TEXT DEFAULT 'mdns',
    discovered_at    TEXT NOT NULL,
    configured       INTEGER DEFAULT 0
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()


def connect() -> aiosqlite.Connection:
    """Returns an unopened connection — caller must `async with` or open/close."""
    return aiosqlite.connect(config.DB_PATH)
