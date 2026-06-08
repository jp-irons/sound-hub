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
    configured       INTEGER DEFAULT 0,
    approval_status  TEXT DEFAULT 'pending'
);
"""

# Values for `approval_status`. New nodes land as PENDING — discovery alone
# (matching the soundcapture-* mDNS hostname pattern) is not enough to admit
# a node into the active set; an operator decision is required. REJECTED
# nodes are retained (not deleted) so a rejection can be reversed later
# without the node needing to be re-discovered from scratch.
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"


async def init_db() -> None:
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.executescript(SCHEMA)

        # Migration: `CREATE TABLE IF NOT EXISTS` doesn't add columns to an
        # already-existing table — an existing sound_hub.db predates
        # `approval_status` and needs it added explicitly.
        cursor = await conn.execute("PRAGMA table_info(nodes)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "approval_status" not in columns:
            await conn.execute(
                "ALTER TABLE nodes ADD COLUMN approval_status TEXT DEFAULT 'pending'"
            )

        # Migration: `approval_status` is new — pre-existing rows (created
        # before this column existed) get NULL from ALTER TABLE's default
        # handling on some SQLite versions, or simply predate the gating
        # concept entirely. Either way they're nodes we already know and
        # trust (they were active before this feature existed), so treat
        # them as already-approved rather than retroactively quarantining
        # hardware that's been running fine.
        await conn.execute(
            "UPDATE nodes SET approval_status = ? WHERE approval_status IS NULL",
            (APPROVED,),
        )
        await conn.commit()


def connect() -> aiosqlite.Connection:
    """Returns an unopened connection — caller must `async with` or open/close."""
    return aiosqlite.connect(config.DB_PATH)
