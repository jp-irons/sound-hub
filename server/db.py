"""SQLite persistence for the node registry.

Only identity/provisioning state is persisted here (a node, once known,
should survive a backend restart). Live status from polling is kept
in-memory in `registry` — it's volatile and re-populated within seconds
of startup.

array_origin holds the hub-level geographic datum for the node array.
It is independent of any node — set by the operator via
POST /api/origin/set-from-node/{node_id} (computed by back-projecting a
node's GPS centroid through its surveyed array offset) or via a manual
PUT /api/origin override.  Once set it survives all node restarts/removals.
"""
import aiosqlite

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE,
    hashed_password TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'viewer',
    created_at      TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    username    TEXT,
    source_ip   TEXT,
    method      TEXT,
    path        TEXT,
    status_code INTEGER
);

CREATE TABLE IF NOT EXISTS detections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT,            -- filename or label (e.g. "upload", node WAV name)
    analyzed_at     TEXT NOT NULL,   -- ISO8601 UTC
    common_name     TEXT NOT NULL,
    scientific_name TEXT NOT NULL,
    confidence      REAL NOT NULL,
    start_sec       REAL,            -- offset within the source file
    end_sec         REAL
);

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

CREATE TABLE IF NOT EXISTS node_positions (
    node_id    TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    pos_e      REAL,
    pos_n      REAL,
    pos_alt    REAL,
    pos_status TEXT DEFAULT 'estimated',
    -- is_origin is now an informational marker only (which node's GPS was used
    -- to establish the hub array_origin).  No uniqueness constraint — the
    -- array origin lives in the array_origin table, not here.
    is_origin  INTEGER DEFAULT 0,
    -- origin_lat/lon/alt retained for schema backward-compat but no longer
    -- read by hub logic; use array_origin table instead.
    origin_lat REAL,
    origin_lon REAL,
    origin_alt REAL,
    -- Operator-surveyed absolute coordinates for this node (optional).
    -- Used as an alternative to live GPS centroid when setting array origin.
    -- Same back-projection math: origin = surveyed_latlon - N/E/Alt_offset.
    surveyed_lat REAL,
    surveyed_lon REAL,
    surveyed_alt REAL,
    updated_at TEXT NOT NULL
);

-- Hub-level geographic datum for the node array.  Always a single row
-- (id=1 enforced by CHECK constraint).  Independent of any specific node.
CREATE TABLE IF NOT EXISTS array_origin (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    lat      REAL NOT NULL,
    lon      REAL NOT NULL,
    alt_m    REAL NOT NULL,
    set_from TEXT,    -- node_id whose GPS centroid was used (audit trail)
    set_at   TEXT NOT NULL
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

        # Migration: drop the old UNIQUE partial index that enforced a single
        # origin node.  is_origin is now an informational marker only — the
        # array origin lives in the array_origin table.
        await conn.execute("DROP INDEX IF EXISTS uq_node_positions_origin")

        # Migration: if array_origin is empty but an old-style origin node
        # exists (is_origin=1 with origin_lat/lon/alt set), seed array_origin
        # from it so existing surveys are not lost on first upgrade.
        cursor = await conn.execute("SELECT COUNT(*) FROM array_origin")
        (origin_count,) = await cursor.fetchone()
        if origin_count == 0:
            cursor = await conn.execute(
                """SELECT node_id, origin_lat, origin_lon, origin_alt
                   FROM node_positions
                   WHERE is_origin = 1
                     AND origin_lat IS NOT NULL
                     AND origin_lon IS NOT NULL
                     AND origin_alt IS NOT NULL
                   LIMIT 1"""
            )
            row = await cursor.fetchone()
            if row:
                node_id, lat, lon, alt = row
                from datetime import datetime, timezone
                await conn.execute(
                    """INSERT OR IGNORE INTO array_origin
                       (id, lat, lon, alt_m, set_from, set_at)
                       VALUES (1, ?, ?, ?, ?, ?)""",
                    (lat, lon, alt, node_id,
                     datetime.now(timezone.utc).isoformat()),
                )

        # Migration: add surveyed_lat/lon/alt columns if they don't exist yet.
        cursor = await conn.execute("PRAGMA table_info(node_positions)")
        pos_columns = {row[1] for row in await cursor.fetchall()}
        for col in ("surveyed_lat", "surveyed_lon", "surveyed_alt"):
            if col not in pos_columns:
                await conn.execute(
                    f"ALTER TABLE node_positions ADD COLUMN {col} REAL"
                )

        await conn.commit()


def connect() -> aiosqlite.Connection:
    """Returns an unopened connection — caller must `async with` or open/close."""
    return aiosqlite.connect(config.DB_PATH)


# ---------------------------------------------------------------------------
# node_positions CRUD
# ---------------------------------------------------------------------------

async def get_node_position(node_id: str) -> dict | None:
    """Return the hub-stored position record for a node, or None if not set."""
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM node_positions WHERE node_id = ?", (node_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def upsert_node_position(
    node_id: str,
    *,
    pos_e: float | None = None,
    pos_n: float | None = None,
    pos_alt: float | None = None,
    pos_status: str = "estimated",
    is_origin: bool = False,
    origin_lat: float | None = None,
    origin_lon: float | None = None,
    origin_alt: float | None = None,
    surveyed_lat: float | None = None,
    surveyed_lon: float | None = None,
    surveyed_alt: float | None = None,
    updated_at: str,
) -> None:
    """Insert or replace the hub-stored position for a node."""
    async with connect() as conn:
        await conn.execute(
            """INSERT OR REPLACE INTO node_positions
               (node_id, pos_e, pos_n, pos_alt, pos_status,
                is_origin, origin_lat, origin_lon, origin_alt,
                surveyed_lat, surveyed_lon, surveyed_alt, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (node_id, pos_e, pos_n, pos_alt, pos_status,
             1 if is_origin else 0,
             origin_lat, origin_lon, origin_alt,
             surveyed_lat, surveyed_lon, surveyed_alt, updated_at),
        )
        await conn.commit()


async def clear_origin() -> None:
    """Clear is_origin marker on all nodes (informational only).
    No-op if no node is marked. Called when the array origin is cleared or
    re-assigned so the display marker stays consistent."""
    async with connect() as conn:
        await conn.execute(
            "UPDATE node_positions SET is_origin = 0 WHERE is_origin = 1"
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# array_origin CRUD  (hub-level geographic datum, independent of any node)
# ---------------------------------------------------------------------------

async def get_array_origin() -> dict | None:
    """Return the hub array origin, or None if not yet set."""
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM array_origin WHERE id = 1")
        row = await cursor.fetchone()
        return dict(row) if row else None


async def set_array_origin(
    *,
    lat: float,
    lon: float,
    alt_m: float,
    set_from: str | None,
    set_at: str,
) -> None:
    """Insert or replace the hub array origin (always row id=1)."""
    async with connect() as conn:
        await conn.execute(
            """INSERT OR REPLACE INTO array_origin (id, lat, lon, alt_m, set_from, set_at)
               VALUES (1, ?, ?, ?, ?, ?)""",
            (lat, lon, alt_m, set_from, set_at),
        )
        await conn.commit()


async def clear_array_origin() -> None:
    """Remove the hub array origin row."""
    async with connect() as conn:
        await conn.execute("DELETE FROM array_origin WHERE id = 1")
        await conn.commit()


# ---------------------------------------------------------------------------
# node_positions bulk read
# ---------------------------------------------------------------------------

async def list_node_positions() -> dict[str, dict]:
    """Return all node position records keyed by node_id."""
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM node_positions")
        rows = await cursor.fetchall()
        return {row["node_id"]: dict(row) for row in rows}


# ---------------------------------------------------------------------------
# User accounts CRUD
# ---------------------------------------------------------------------------

async def count_users() -> int:
    """Return the total number of active users."""
    async with connect() as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM users WHERE active = 1")
        (count,) = await cursor.fetchone()
        return count


async def get_user(username: str) -> dict | None:
    """Return a user by username, or None if not found / inactive."""
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM users WHERE username = ? AND active = 1", (username,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def create_user(
    username: str, hashed_password: str, role: str, created_at: str
) -> None:
    """Insert a new active user."""
    async with connect() as conn:
        await conn.execute(
            """INSERT INTO users (username, hashed_password, role, created_at, active)
               VALUES (?, ?, ?, ?, 1)""",
            (username, hashed_password, role, created_at),
        )
        await conn.commit()


async def list_users() -> list[dict]:
    """Return all active users ordered by creation date."""
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT username, role, created_at FROM users WHERE active = 1 ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def count_active_admins() -> int:
    """Return the number of active admin users."""
    async with connect() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
        )
        (count,) = await cursor.fetchone()
        return count


async def delete_user(username: str) -> None:
    """Soft-delete a user (sets active=0)."""
    async with connect() as conn:
        await conn.execute(
            "UPDATE users SET active = 0 WHERE username = ?", (username,)
        )
        await conn.commit()


async def update_user_password(username: str, hashed_password: str) -> bool:
    """Update a user's password. Returns True if the user was found and updated."""
    async with connect() as conn:
        cursor = await conn.execute(
            "UPDATE users SET hashed_password = ? WHERE username = ? AND active = 1",
            (hashed_password, username),
        )
        await conn.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Detections CRUD
# ---------------------------------------------------------------------------

async def insert_detections(
    source: str, analyzed_at: str, detections: list[dict]
) -> None:
    """Bulk-insert BirdNET detection rows."""
    async with connect() as conn:
        await conn.executemany(
            """INSERT INTO detections
               (source, analyzed_at, common_name, scientific_name,
                confidence, start_sec, end_sec)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    source,
                    analyzed_at,
                    d["common_name"],
                    d["scientific_name"],
                    d["confidence"],
                    d.get("start_time"),
                    d.get("end_time"),
                )
                for d in detections
            ],
        )
        await conn.commit()


async def list_detections(limit: int = 200) -> list[dict]:
    """Return the most recent detections ordered newest-first."""
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT * FROM detections
               ORDER BY analyzed_at DESC, id DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
