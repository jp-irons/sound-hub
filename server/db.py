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
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

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

-- TDOA orchestration attempts — one row per persisted top-species detection
-- in audio_push() that triggers planning (see species_tdoa_pipeline design,
-- sound-hub/DESIGN.md "Milestones"). Milestone 1 only ever writes
-- status='planned' rows recording the plan (pull window, candidate
-- neighbours) without issuing any pull. Later milestones (2-4) advance
-- status and will likely need new columns (per-node requestId/ack/arrival
-- timestamp, solve result) added via ALTER TABLE migrations below, same
-- pattern as the rest of this file — not designed yet, deliberately deferred
-- until the milestone that needs them.
--
-- correlation_method/onset_detection_method/min_corroborating_nodes are
-- copied from species_tdoa_params at plan time rather than joined live, so
-- a later edit to that table doesn't retroactively change what an
-- already-planned attempt says it used.
--
-- planned_node_ids is a JSON array of candidate neighbour node_ids — the
-- plan as it stood at planning time. Per-node *execution* state (requestId,
-- ack/pull outcome) lives in tdoa_attempt_nodes below (added milestone 2)
-- rather than mutating this column, so the original plan stays a stable
-- historical record even if a pull is retried or partially fails.
CREATE TABLE IF NOT EXISTS tdoa_attempts (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    audio_event_id          INTEGER REFERENCES audio_events(id) ON DELETE CASCADE,
    origin_node_id          TEXT,
    species_key             TEXT NOT NULL,
    used_default            INTEGER NOT NULL DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT 'planned',
    t_start_us              INTEGER NOT NULL,
    t_end_us                INTEGER NOT NULL,
    planned_node_ids        TEXT NOT NULL,
    min_corroborating_nodes INTEGER NOT NULL,
    correlation_method      TEXT NOT NULL,
    onset_detection_method  TEXT NOT NULL,
    travel_time_floor_s     REAL NOT NULL,
    failure_reason          TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

-- Per-neighbour pull execution state for a tdoa_attempts row (milestone 2).
-- One row per node that planning selected. request_id is NULL when the pull
-- could not even be issued (e.g. node unreachable, broker down) — status
-- distinguishes 'requested' (relay accepted it; the WAV is awaited via the
-- normal audio_push()/requestId mechanism) from 'request_failed' (error
-- holds why). Milestone 3 will update status further as WAVs land and
-- arrival timestamps are derived; not done by this table yet.
CREATE TABLE IF NOT EXISTS tdoa_attempt_nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id  INTEGER NOT NULL REFERENCES tdoa_attempts(id) ON DELETE CASCADE,
    node_id     TEXT NOT NULL,
    request_id  INTEGER,
    status      TEXT NOT NULL,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
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

# trigger_events is diagnostic (near-miss/fire) data, not the permanent
# record — detections/audio_events are. It reached 18M+ rows / 2.3GB with no
# retention policy at all (see idx_trigger_events_t_us migration above),
# which was slow enough to trip upstream request timeouts. 7 days keeps a
# window wide enough to debug a report that comes in a day or two after the
# fact, without letting the table regrow unbounded. Adjust here if it proves
# too short/long in practice — not read from config, deliberately, to avoid
# a footgun where an existing deployment's soundhub.conf doesn't define it.
TRIGGER_EVENTS_RETENTION_DAYS = 7

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
        # WAL is a persistent, on-disk setting (stored in the db file header)
        # — setting it once here means every connection opened later via
        # connect(), including from other processes, is already in WAL mode.
        # This lets readers and a writer proceed concurrently instead of
        # blocking each other, which is what caused the intermittent
        # "database is locked" 500s on /api/nodes/register and the poller's
        # trigger_events inserts.
        await conn.execute("PRAGMA journal_mode=WAL")
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

        # Migration: trigger_events only had an implicit index on
        # (node_id, t_us) from its UNIQUE constraint, which isn't usable for
        # ORDER BY t_us DESC across all nodes (the common case — no node_id
        # filter). At scale (observed: 18M+ rows) that meant a full table
        # scan plus a temp b-tree sort on every /analytics/trigger-diag call,
        # slow enough to trip upstream 504s. IF NOT EXISTS makes this a no-op
        # on every startup after the first; the first run against an
        # existing large table will take noticeably longer as it builds the
        # index — expected, not a hang.
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trigger_events_t_us "
            "ON trigger_events(t_us DESC, id DESC)"
        )

        await conn.commit()


@asynccontextmanager
async def connect():
    """Async context manager yielding an aiosqlite connection.

    Sets busy_timeout on every connection (per-connection setting, unlike
    journal_mode which persists in the db file) so a writer that finds the
    database locked by another connection waits up to 5s and retries instead
    of immediately raising `sqlite3.OperationalError: database is locked`.
    """
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout = 5000")
        yield conn


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
) -> int:
    """Record one push received at POST /api/audio/push, regardless of outcome.

    analysis_status is one of 'analyzed' | 'skipped_not_ready' | 'error'.
    t_start_us/t_end_us are the node-clock capture window — present for
    triggered (node-initiated) pushes, NULL for hub-requested pulls (the hub
    already has those from its own request).

    Returns the inserted row's id — used by the TDOA orchestration hook in
    routes.py to link a tdoa_attempts row back to the audio_event that
    triggered it.
    """
    async with connect() as conn:
        cursor = await conn.execute(
            """INSERT INTO audio_events
               (node_id, triggered, received_at, bytes, analysis_status,
                detection_count, top_confidence, top_species, t_start_us, t_end_us)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (node_id, 1 if triggered else 0, received_at, bytes_, analysis_status,
             detection_count, top_confidence, top_species, t_start_us, t_end_us),
        )
        await conn.commit()
        return cursor.lastrowid


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


async def prune_trigger_events(retention_days: int = TRIGGER_EVENTS_RETENTION_DAYS) -> int:
    """Delete trigger_events rows older than retention_days. Returns the
    number of rows deleted.

    t_us is node-clock UTC microseconds (not a hub wall-clock timestamp), so
    the cutoff is computed the same way for a correct comparison. Intended
    to be called periodically from the poller (see poller.run()) rather than
    on every poll tick — a day-granularity retention window doesn't need
    sub-minute precision, and this table is large enough that a tighter
    cadence would just be unnecessary write churn.

    The very first call against an already-large, never-pruned table can
    delete a large fraction of it in one transaction — consider running an
    equivalent one-off `DELETE FROM trigger_events WHERE t_us < ...` via the
    sqlite3 CLI at a quiet moment first, same as the index migration above,
    rather than letting it happen cold on a live server.
    """
    cutoff_us = int(
        (datetime.now(timezone.utc) - timedelta(days=retention_days)).timestamp()
        * 1_000_000
    )
    async with connect() as conn:
        cursor = await conn.execute(
            "DELETE FROM trigger_events WHERE t_us < ?", (cutoff_us,)
        )
        await conn.commit()
        return cursor.rowcount if cursor.rowcount is not None and cursor.rowcount > 0 else 0


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


# ---------------------------------------------------------------------------
# TDOA attempts CRUD
# ---------------------------------------------------------------------------

async def insert_tdoa_attempt(
    *,
    audio_event_id: int,
    origin_node_id: str | None,
    species_key: str,
    used_default: bool,
    status: str,
    t_start_us: int,
    t_end_us: int,
    planned_node_ids: str,
    min_corroborating_nodes: int,
    correlation_method: str,
    onset_detection_method: str,
    travel_time_floor_s: float,
    failure_reason: str | None = None,
) -> int:
    """Insert one TDOA orchestration attempt. Always called with status=
    'planned' at insert time (the row's status is advanced afterwards via
    update_tdoa_attempt_status once milestone 2's pulls have been issued).
    Returns the inserted row's id.

    planned_node_ids is a pre-serialized JSON array string — this module
    stays a dumb DB layer and has no opinion on its contents, matching the
    rest of this file's style.
    """
    now = datetime.now(timezone.utc).isoformat()
    async with connect() as conn:
        cursor = await conn.execute(
            """INSERT INTO tdoa_attempts
               (audio_event_id, origin_node_id, species_key, used_default,
                status, t_start_us, t_end_us, planned_node_ids,
                min_corroborating_nodes, correlation_method,
                onset_detection_method, travel_time_floor_s, failure_reason,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (audio_event_id, origin_node_id, species_key, 1 if used_default else 0,
             status, t_start_us, t_end_us, planned_node_ids,
             min_corroborating_nodes, correlation_method, onset_detection_method,
             travel_time_floor_s, failure_reason, now, now),
        )
        await conn.commit()
        return cursor.lastrowid


async def list_tdoa_attempts(limit: int = 200) -> list[dict]:
    """Return the most recent TDOA attempts, newest first. No API route
    exposes this yet (milestone 1 is DB-inspectable only) — kept for
    symmetry with this file's other tables and for use by later milestones."""
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM tdoa_attempts ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def update_tdoa_attempt_status(
    attempt_id: int, status: str, failure_reason: str | None = None
) -> None:
    """Advance a tdoa_attempts row's status (milestone 2: 'planned' ->
    'pulling' once at least one neighbour pull is issued, or -> 'failed' if
    none could be). failure_reason is left untouched (not overwritten with
    NULL) when not given, so a later successful status update doesn't erase
    a previously recorded reason."""
    now = datetime.now(timezone.utc).isoformat()
    async with connect() as conn:
        if failure_reason is not None:
            await conn.execute(
                "UPDATE tdoa_attempts SET status = ?, failure_reason = ?, "
                "updated_at = ? WHERE id = ?",
                (status, failure_reason, now, attempt_id),
            )
        else:
            await conn.execute(
                "UPDATE tdoa_attempts SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, attempt_id),
            )
        await conn.commit()


async def insert_tdoa_attempt_node(
    *,
    attempt_id: int,
    node_id: str,
    request_id: int | None,
    status: str,
    error: str | None = None,
) -> int:
    """Insert one per-neighbour pull execution record against a tdoa_attempts
    row (milestone 2). Returns the inserted row's id. request_id is None when
    the pull could never be issued (error holds why)."""
    now = datetime.now(timezone.utc).isoformat()
    async with connect() as conn:
        cursor = await conn.execute(
            """INSERT INTO tdoa_attempt_nodes
               (attempt_id, node_id, request_id, status, error,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (attempt_id, node_id, request_id, status, error, now, now),
        )
        await conn.commit()
        return cursor.lastrowid


async def list_tdoa_attempt_nodes(attempt_id: int) -> list[dict]:
    """Return all per-neighbour pull records for one TDOA attempt. No API
    route exposes this yet — DB-inspectable only, same as tdoa_attempts
    itself at this milestone."""
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM tdoa_attempt_nodes WHERE attempt_id = ? ORDER BY id",
            (attempt_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
