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
import logging
from datetime import datetime, timezone

import aiosqlite

from . import config

log = logging.getLogger("sound_hub.db")

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
    node_id         TEXT,            -- nodes.id (hostname) that captured the audio;
                                      -- NULL for manual uploads with no node origin
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

-- One row per audio push received at POST /api/audio/push, regardless of
-- BirdNET outcome.  This is the diagnostic record that list_detections()
-- can't provide: detections are only written when BirdNET clears the
-- persisted-detection confidence threshold, so a node that pushes audio but
-- never gets a hit (trigger too strict, threshold too strict, etc.) leaves
-- no trace anywhere else.  top_confidence/top_species capture BirdNET's
-- single best candidate for the chunk even when it falls below threshold —
-- the "near miss" signal needed to tell trigger-sensitivity problems apart
-- from BirdNET-threshold problems.
CREATE TABLE IF NOT EXISTS audio_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id          TEXT,            -- nodes.id (hostname); NULL if srcMac didn't map to a known node
    triggered        INTEGER NOT NULL, -- 1 = self-triggered push (no requestId), 0 = hub-requested pull
    received_at      TEXT NOT NULL,   -- ISO8601 UTC, when the push hit the hub
    bytes            INTEGER NOT NULL,
    analysis_status  TEXT NOT NULL,   -- 'analyzed' | 'skipped_not_ready' | 'error'
    detection_count   INTEGER DEFAULT 0,  -- rows actually persisted to `detections` (>= threshold)
    top_confidence    REAL,               -- best raw candidate confidence, any threshold (NULL if no candidates at all)
    top_species       TEXT,               -- common_name of the top candidate, NULL if none
    t_start_us        INTEGER,            -- capture-window start, node-clock Unix epoch us (NULL for hub-requested pulls predating this column, or if a node hasn't sent it)
    t_end_us          INTEGER             -- capture-window end, node-clock Unix epoch us
);

-- Per-block AudioTrigger dual-gate ratios pulled from a node's
-- GET /app/api/trigger-diag (see TriggerDiagnostics.hpp on the node side).
-- Only "interesting" blocks are ever recorded by the node (either gate
-- ratio >= 1.5, or the trigger fired) — this is not a continuous log of
-- every audio block, just the near-misses and confirmed fires needed to
-- diagnose why the v2 dual-gate trigger (band energy + spectral flux) does
-- or doesn't fire for a given call. UNIQUE(node_id, t_us) + INSERT OR IGNORE
-- on the poller side de-duplicates rows seen across overlapping poll cycles
-- (the node's ring buffer can still hold a block from the previous poll).
CREATE TABLE IF NOT EXISTS trigger_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id      TEXT,            -- nodes.id (hostname)
    t_us         INTEGER NOT NULL, -- node-clock UTC microseconds for this block
    energy_ratio REAL NOT NULL,
    flux_ratio   REAL NOT NULL,
    fired        INTEGER NOT NULL, -- 1 if this block was a debounced trigger fire
    inserted_at  TEXT NOT NULL,    -- ISO8601 UTC, when the hub ingested this row
    UNIQUE(node_id, t_us)
);

-- Per-species TDOA orchestration tuning (CRUDable so parameters can be
-- adjusted without a backend redeploy — see species-TDOA-pipeline design
-- discussion). species_key matches detections.common_name. The
-- '__default__' sentinel row (DEFAULT_SPECIES_KEY below) is the fallback for
-- any species with no row of its own, or a disabled one — see
-- get_effective_species_tdoa_params(). It is seeded by init_db() and
-- protected from delete/disable at the route layer (server/routes.py); this
-- module's CRUD functions have no opinion on that, to stay a dumb DB layer.
CREATE TABLE IF NOT EXISTS species_tdoa_params (
    species_key             TEXT PRIMARY KEY,
    enabled                 INTEGER NOT NULL DEFAULT 1,
    correlation_method      TEXT NOT NULL DEFAULT 'gcc_phat',
    onset_detection_method  TEXT NOT NULL DEFAULT 'global_peak',
    freq_band_low_hz        REAL,
    freq_band_high_hz       REAL,
    pull_window_s           REAL NOT NULL DEFAULT 3.0,
    window_margin_pre_ms    REAL NOT NULL DEFAULT 500.0,
    window_margin_post_ms   REAL NOT NULL DEFAULT 500.0,
    min_corroborating_nodes INTEGER NOT NULL DEFAULT 4,
    notes                   TEXT,
    updated_at              TEXT NOT NULL
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

# Sentinel species_key for the fallback row in species_tdoa_params — used
# when a detected species has no row of its own, or its row is disabled.
# Not seeded by init_db() — created lazily by
# get_effective_species_tdoa_params() the first time it's needed, from
# FACTORY_DEFAULT_SPECIES_PARAMS below. Protected from delete/disable at the
# route layer.
DEFAULT_SPECIES_KEY = "__default__"

# Known-good values for the __default__ row, used both to lazily create it
# (get_effective_species_tdoa_params) and to restore it on demand
# (reset_species_tdoa_params_to_factory_default) if an operator tunes it
# into something broken. Keys match upsert_species_tdoa_params()'s kwargs
# (minus species_key/updated_at).
FACTORY_DEFAULT_SPECIES_PARAMS = {
    "enabled": True,
    "correlation_method": "gcc_phat",
    "onset_detection_method": "global_peak",
    "freq_band_low_hz": None,
    "freq_band_high_hz": None,
    "pull_window_s": 3.0,
    "window_margin_pre_ms": 500.0,
    "window_margin_post_ms": 500.0,
    "min_corroborating_nodes": 4,
    "notes": None,
}


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

        # Migration: add node_id to detections if it doesn't exist yet —
        # `CREATE TABLE IF NOT EXISTS` doesn't touch an already-existing table.
        cursor = await conn.execute("PRAGMA table_info(detections)")
        detection_columns = {row[1] for row in await cursor.fetchall()}
        if "node_id" not in detection_columns:
            await conn.execute("ALTER TABLE detections ADD COLUMN node_id TEXT")

        # Migration: add t_start_us/t_end_us to audio_events if they don't
        # exist yet — needed to anchor a triggered push's capture window
        # precisely (received_at is only hub-arrival wall-clock).
        cursor = await conn.execute("PRAGMA table_info(audio_events)")
        audio_event_columns = {row[1] for row in await cursor.fetchall()}
        if "t_start_us" not in audio_event_columns:
            await conn.execute("ALTER TABLE audio_events ADD COLUMN t_start_us INTEGER")
        if "t_end_us" not in audio_event_columns:
            await conn.execute("ALTER TABLE audio_events ADD COLUMN t_end_us INTEGER")

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


async def set_node_as_origin(node_id: str) -> None:
    """Mark node_id as the origin node, clearing any previous marker.
    Uses a single transaction so the one-origin invariant is never violated
    even transiently."""
    async with connect() as conn:
        await conn.execute("UPDATE node_positions SET is_origin = 0")
        await conn.execute(
            "UPDATE node_positions SET is_origin = 1 WHERE node_id = ?",
            (node_id,),
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
    source: str, analyzed_at: str, detections: list[dict],
    node_id: str | None = None,
) -> None:
    """Bulk-insert BirdNET detection rows.

    node_id identifies which node's audio this came from (matches nodes.id,
    i.e. hostname). None for detections with no node origin (manual WAV
    upload via /api/detections/analyze).
    """
    async with connect() as conn:
        await conn.executemany(
            """INSERT INTO detections
               (source, node_id, analyzed_at, common_name, scientific_name,
                confidence, start_sec, end_sec)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    source,
                    node_id,
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


async def list_detections(
    limit: int = 200,
    min_conf: float = 0.0,
    species: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> list[dict]:
    """Return the most recent detections ordered newest-first.

    min_conf filters on confidence >= value. species matches against either
    common_name or scientific_name (SQLite LIKE is case-insensitive for
    ASCII). from_ts/to_ts are inclusive bounds on analyzed_at, expected as
    ISO8601 strings comparable lexicographically with the stored UTC values
    (e.g. produced by JS Date.toISOString()).
    """
    where = ["confidence >= ?"]
    params: list = [min_conf]

    if species:
        where.append("(common_name LIKE ? OR scientific_name LIKE ?)")
        like = f"%{species}%"
        params.extend([like, like])

    if from_ts:
        where.append("analyzed_at >= ?")
        params.append(from_ts)

    if to_ts:
        where.append("analyzed_at <= ?")
        params.append(to_ts)

    params.append(limit)

    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            f"""SELECT * FROM detections
                WHERE {' AND '.join(where)}
                ORDER BY analyzed_at DESC, id DESC LIMIT ?""",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def insert_audio_event(
    *,
    node_id: str | None,
    triggered: bool,
    received_at: str,
    bytes_: int,
    analysis_status: str,
    detection_count: int = 0,
    top_confidence: float | None = None,
    top_species: str | None = None,
    t_start_us: int | None = None,
    t_end_us: int | None = None,
) -> None:
    """Record one push received at POST /api/audio/push, regardless of outcome.

    analysis_status is one of 'analyzed' | 'skipped_not_ready' | 'error'.
    t_start_us/t_end_us are the node-clock capture window — present for
    triggered (node-initiated) pushes, NULL for hub-requested pulls (the hub
    already has those from its own request).
    """
    async with connect() as conn:
        await conn.execute(
            """INSERT INTO audio_events
               (node_id, triggered, received_at, bytes, analysis_status,
                detection_count, top_confidence, top_species, t_start_us, t_end_us)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (node_id, 1 if triggered else 0, received_at, bytes_, analysis_status,
             detection_count, top_confidence, top_species, t_start_us, t_end_us),
        )
        await conn.commit()


async def list_audio_events(
    limit: int = 200,
    node_id: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> list[dict]:
    """Return the most recent audio push events, newest first.

    Same from_ts/to_ts semantics as list_detections (inclusive ISO8601
    bounds on received_at).
    """
    where = ["1=1"]
    params: list = []

    if node_id:
        where.append("node_id = ?")
        params.append(node_id)
    if from_ts:
        where.append("received_at >= ?")
        params.append(from_ts)
    if to_ts:
        where.append("received_at <= ?")
        params.append(to_ts)

    params.append(limit)

    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            f"""SELECT * FROM audio_events
                WHERE {' AND '.join(where)}
                ORDER BY received_at DESC, id DESC LIMIT ?""",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def audio_event_summary(
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> list[dict]:
    """Aggregate audio_events per node — feeds the Analytics tab's stat cards.

    Returns one row per node_id with: total pushes, triggered-push count,
    pushes with >=1 detection, pushes with zero detections, last push time,
    last trigger time (triggered=1 only), and the average top_confidence
    among zero-detection pushes (the "how close were the misses" signal —
    NULL if there are no zero-detection pushes in range).
    """
    where = ["1=1"]
    params: list = []
    if from_ts:
        where.append("received_at >= ?")
        params.append(from_ts)
    if to_ts:
        where.append("received_at <= ?")
        params.append(to_ts)

    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            f"""SELECT
                    node_id,
                    COUNT(*) AS total_pushes,
                    SUM(triggered) AS triggered_pushes,
                    SUM(CASE WHEN detection_count > 0 THEN 1 ELSE 0 END) AS pushes_with_detections,
                    SUM(CASE WHEN detection_count = 0 THEN 1 ELSE 0 END) AS pushes_zero_detections,
                    MAX(received_at) AS last_push_at,
                    MAX(CASE WHEN triggered = 1 THEN received_at ELSE NULL END) AS last_trigger_at,
                    AVG(CASE WHEN detection_count = 0 THEN top_confidence ELSE NULL END) AS avg_near_miss_confidence
                FROM audio_events
                WHERE {' AND '.join(where)}
                GROUP BY node_id
                ORDER BY total_pushes DESC""",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def insert_trigger_events(node_id: str | None, rows: list[dict]) -> int:
    """Bulk-insert trigger-diagnostic rows pulled from one node's poll.

    Each row is {"t_us": int, "energy_ratio": float, "flux_ratio": float,
    "fired": bool} — see TriggerDiagHandler's CSV format on the node side.
    INSERT OR IGNORE on (node_id, t_us) silently drops rows already seen on
    a previous poll (the node's ring buffer overlaps poll cycles by design,
    so the hub will usually see most rows more than once).

    Returns the number of rows actually inserted (new rows only).
    """
    if not rows:
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()
    async with connect() as conn:
        cursor = await conn.executemany(
            """INSERT OR IGNORE INTO trigger_events
               (node_id, t_us, energy_ratio, flux_ratio, fired, inserted_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (node_id, r["t_us"], r["energy_ratio"], r["flux_ratio"],
                 1 if r["fired"] else 0, now_iso)
                for r in rows
            ],
        )
        await conn.commit()
        return cursor.rowcount if cursor.rowcount is not None and cursor.rowcount > 0 else 0


async def list_trigger_events(
    limit: int = 500,
    node_id: str | None = None,
    fired_only: bool = False,
) -> list[dict]:
    """Return the most recent trigger-diagnostic rows, newest (by t_us) first."""
    where = ["1=1"]
    params: list = []

    if node_id:
        where.append("node_id = ?")
        params.append(node_id)
    if fired_only:
        where.append("fired = 1")

    params.append(limit)

    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            f"""SELECT * FROM trigger_events
                WHERE {' AND '.join(where)}
                ORDER BY t_us DESC, id DESC LIMIT ?""",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def trigger_event_summary() -> list[dict]:
    """Aggregate trigger_events per node — counts of near-misses vs. fires.

    "Near miss" here means a row was recorded (so at least one gate reached
    the node's interesting-ratio threshold) but fired = 0.
    """
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT
                   node_id,
                   COUNT(*) AS total_rows,
                   SUM(fired) AS fired_rows,
                   SUM(CASE WHEN fired = 0 THEN 1 ELSE 0 END) AS near_miss_rows,
                   AVG(energy_ratio) AS avg_energy_ratio,
                   AVG(flux_ratio) AS avg_flux_ratio,
                   MAX(t_us) AS last_t_us
               FROM trigger_events
               GROUP BY node_id
               ORDER BY total_rows DESC"""
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def list_species_summary(
    min_conf: float = 0.0,
    species: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> list[dict]:
    """Aggregate detections per species (common_name + scientific_name),
    ordered most-frequent-first.

    Same filter semantics as list_detections (species substring-matches
    either name field; from_ts/to_ts are inclusive ISO8601 bounds). No
    time-of-day filtering here — that classification is per-row and depends
    on each detection's own calendar date, so it can't be expressed as a
    SQL range; callers needing it (routes.py) fetch raw rows, classify them,
    and aggregate in Python instead of calling this function.
    """
    where = ["confidence >= ?"]
    params: list = [min_conf]

    if species:
        where.append("(common_name LIKE ? OR scientific_name LIKE ?)")
        like = f"%{species}%"
        params.extend([like, like])

    if from_ts:
        where.append("analyzed_at >= ?")
        params.append(from_ts)

    if to_ts:
        where.append("analyzed_at <= ?")
        params.append(to_ts)

    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            f"""SELECT common_name, scientific_name,
                       COUNT(*) AS count,
                       MAX(analyzed_at) AS last_seen,
                       AVG(confidence) AS avg_confidence
                FROM detections
                WHERE {' AND '.join(where)}
                GROUP BY common_name, scientific_name
                ORDER BY count DESC""",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Species TDOA params CRUD
# ---------------------------------------------------------------------------

async def get_species_tdoa_params(species_key: str) -> dict | None:
    """Return one species' TDOA params row, or None if not configured."""
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM species_tdoa_params WHERE species_key = ?",
            (species_key,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def list_species_tdoa_params() -> list[dict]:
    """Return all species TDOA params rows, including the __default__
    sentinel, ordered by species_key."""
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM species_tdoa_params ORDER BY species_key"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def upsert_species_tdoa_params(
    species_key: str,
    *,
    enabled: bool,
    correlation_method: str,
    onset_detection_method: str,
    freq_band_low_hz: float | None,
    freq_band_high_hz: float | None,
    pull_window_s: float,
    window_margin_pre_ms: float,
    window_margin_post_ms: float,
    min_corroborating_nodes: int,
    notes: str | None,
    updated_at: str,
) -> None:
    """Insert or replace one species' TDOA params row (including the
    __default__ sentinel — this function has no opinion on protecting it;
    that's a route-layer concern)."""
    async with connect() as conn:
        await conn.execute(
            """INSERT OR REPLACE INTO species_tdoa_params
               (species_key, enabled, correlation_method, onset_detection_method,
                freq_band_low_hz, freq_band_high_hz, pull_window_s,
                window_margin_pre_ms, window_margin_post_ms,
                min_corroborating_nodes, notes, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (species_key, 1 if enabled else 0, correlation_method,
             onset_detection_method, freq_band_low_hz, freq_band_high_hz,
             pull_window_s, window_margin_pre_ms, window_margin_post_ms,
             min_corroborating_nodes, notes, updated_at),
        )
        await conn.commit()


async def delete_species_tdoa_params(species_key: str) -> bool:
    """Delete one species' TDOA params row. Returns True if a row was deleted.

    Callers must block deleting DEFAULT_SPECIES_KEY themselves (route layer)
    — this function has no opinion on that, keeping this module a dumb CRUD
    surface consistent with the rest of its functions.
    """
    async with connect() as conn:
        cursor = await conn.execute(
            "DELETE FROM species_tdoa_params WHERE species_key = ?",
            (species_key,),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def get_effective_species_tdoa_params(species_key: str) -> tuple[dict, bool]:
    """Return (params, used_default) for a detected species.

    Falls back to the __default__ sentinel row if species_key has no row, or
    its row has enabled=0. Logs a warning on fallback so an operator tailing
    the hub log notices a species running on best-guess defaults rather than
    deliberately-tuned params. The TDOA orchestration pipeline (not yet
    built) should persist used_default on whatever record holds the attempt,
    for the same reason — this function only covers the lookup half of that.

    The __default__ row itself is not seeded by init_db() — it's created
    lazily here, from FACTORY_DEFAULT_SPECIES_PARAMS, the first time any
    species needs to fall back and finds it missing.
    """
    row = await get_species_tdoa_params(species_key)
    if row is not None and row["enabled"]:
        return row, False

    default_row = await get_species_tdoa_params(DEFAULT_SPECIES_KEY)
    if default_row is None:
        log.warning(
            "species_tdoa_params: '%s' sentinel row missing, creating from "
            "factory defaults", DEFAULT_SPECIES_KEY,
        )
        await upsert_species_tdoa_params(
            DEFAULT_SPECIES_KEY,
            updated_at=datetime.now(timezone.utc).isoformat(),
            **FACTORY_DEFAULT_SPECIES_PARAMS,
        )
        default_row = await get_species_tdoa_params(DEFAULT_SPECIES_KEY)

    reason = "disabled" if row is not None else "unconfigured"
    log.warning(
        "species_tdoa_params: '%s' is %s, falling back to %s defaults",
        species_key, reason, DEFAULT_SPECIES_KEY,
    )
    return default_row, True


async def reset_species_tdoa_params_to_factory_default() -> dict:
    """Overwrite the __default__ row with FACTORY_DEFAULT_SPECIES_PARAMS,
    regardless of its current state. The recovery path for when __default__
    has been tuned into something broken — unlike the lazy-create in
    get_effective_species_tdoa_params(), this runs unconditionally, not only
    when the row is missing.
    """
    await upsert_species_tdoa_params(
        DEFAULT_SPECIES_KEY,
        updated_at=datetime.now(timezone.utc).isoformat(),
        **FACTORY_DEFAULT_SPECIES_PARAMS,
    )
    log.info("species_tdoa_params: '%s' reset to factory defaults", DEFAULT_SPECIES_KEY)
    return await get_species_tdoa_params(DEFAULT_SPECIES_KEY)
