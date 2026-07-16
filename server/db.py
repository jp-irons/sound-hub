"""SQLite persistence for the node registry.

Only identity/provisioning state is persisted here (a node, once known,
should survive a backend restart). Live status from polling is kept
in-memory in `registry` — it's volatile and re-populated within seconds
of startup.

array_origin holds the hub-level geographic datum for the node array.
It is independent of any node — set by the operator via
POST /api/origin/set-from-node/{node_id} (computed by back-projecting a
node's live GPS EMA — an in-memory hub-side average, see registry.py —
through its surveyed array offset) or via a manual PUT /api/origin
override.  Once set it survives all node restarts/removals.
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
    discovery_method TEXT DEFAULT 'manual',
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

-- Governs audio_cleanup.py's periodic sweep of the audio/ directory (TDOA
-- pull segments) — age-based and absolute-size pruning, oldest files first.
-- Always a single row (id=1 enforced by CHECK constraint), seeded by
-- init_db() with AUDIO_RETENTION_HOURS_DEFAULT/AUDIO_MAX_SIZE_BYTES_DEFAULT
-- below so audio_cleanup.py never has to handle a missing-row case.
-- Minimums (3h / 1GB) are enforced at the API boundary (models.py's Field
-- constraints), not here — this table stays a dumb value store, same
-- convention as species_tdoa_params.
CREATE TABLE IF NOT EXISTS audio_cleanup_settings (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    retention_hours REAL NOT NULL,
    max_size_bytes  INTEGER NOT NULL,
    updated_at      TEXT NOT NULL
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
                                      -- | 'skipped_birdnet_tdoa_pull' (pull was
                                      -- for TDOA corroboration only — species
                                      -- already known, BirdNET deliberately not
                                      -- re-run; see routes.py audio_push())
                                      -- NOTE: rows written before 2026-07-11
                                      -- may still carry the old value
                                      -- 'skipped_tdoa_corroboration'.
    detection_count   INTEGER DEFAULT 0,  -- rows actually persisted to `detections` (>= threshold)
    top_confidence    REAL,               -- best raw candidate confidence, any threshold (NULL if no candidates at all)
    top_species       TEXT,               -- common_name of the top candidate, NULL if none
    t_start_us        INTEGER,            -- capture-window start, node-clock Unix epoch us (NULL for hub-requested pulls predating this column, or if a node hasn't sent it)
    t_end_us          INTEGER,            -- capture-window end, node-clock Unix epoch us
    filename          TEXT                -- WAV filename under the audio/ dir (see routes.py
                                            -- audio_push()'s fname). NULL for rows written before
                                            -- this column existed. Needed so TDOA correlation
                                            -- (milestone 3) can re-open a node's WAV after the
                                            -- fact — e.g. a "reused_existing" corroborator whose
                                            -- audio arrived long before the attempt that ends up
                                            -- using it, or the origin node's own trigger push.
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

-- One row per (node, 1-minute bucket), aggregating trigger_events rows once
-- they're old enough to be considered settled (see
-- TRIGGER_EVENT_ROLLUP_SAFETY_MARGIN_US). Exists so the near-miss "shape"
-- of the data (rate, ratio range) survives long after the raw per-block
-- rows that produced it have been pruned — see rollup_trigger_events() for
-- the aggregation query and prune_trigger_events() for raw-row cleanup.
-- bucket_start_us is t_us truncated down to the minute, in the same
-- node-clock UTC microsecond units as trigger_events.t_us.
CREATE TABLE IF NOT EXISTS trigger_event_rollups (
    node_id          TEXT NOT NULL,
    bucket_start_us  INTEGER NOT NULL,
    entry_count      INTEGER NOT NULL, -- rows in this bucket (trigger_events only ever holds "interesting" blocks, so this is near-miss+fired count, not total processed blocks)
    fired_count      INTEGER NOT NULL,
    energy_ratio_min REAL NOT NULL,
    energy_ratio_avg REAL NOT NULL,
    energy_ratio_max REAL NOT NULL,
    flux_ratio_min   REAL NOT NULL,
    flux_ratio_avg   REAL NOT NULL,
    flux_ratio_max   REAL NOT NULL,
    rolled_up_at     TEXT NOT NULL,
    PRIMARY KEY (node_id, bucket_start_us)
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
    onset_threshold_factor  REAL NOT NULL DEFAULT 8.0,
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
-- correlation_method/onset_detection_method/min_corroborating_nodes/
-- onset_threshold_factor/freq_band_low_hz/freq_band_high_hz are copied from
-- species_tdoa_params at plan time rather than joined live, so a later edit
-- to that table doesn't retroactively change what an already-planned
-- attempt says it used.
--
-- planned_node_ids is a JSON array of candidate neighbour node_ids — the
-- plan as it stood at planning time. Per-node *execution* state (requestId,
-- ack/pull outcome) lives in tdoa_attempt_nodes below (added milestone 2)
-- rather than mutating this column, so the original plan stays a stable
-- historical record even if a pull is retried or partially fails.
--
-- solved_e/solved_n/solved_alt/solve_residual_m/solve_method (milestone 4):
-- filled by persist_tdoa_solution() once >= min_corroborating_nodes rows in
-- tdoa_attempt_nodes have an arrival_us, status moves to 'solved'.
-- solve_ambiguous_json holds the mirror root's [x,y,z] for a 4-node
-- (quadratic) solve as a JSON array, NULL for a 5+-node (least-squares)
-- solve where there is no ambiguity — see tdoa_solver.py. No hint_point is
-- wired into the automatic solve yet (nothing in production config defines
-- one today), so a 4-node solve's ambiguity is stored, not auto-resolved —
-- see project_soundhub_tdoa_dedup / DESIGN.md gaps.
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
    onset_threshold_factor  REAL NOT NULL DEFAULT 8.0,
    freq_band_low_hz        REAL,
    freq_band_high_hz       REAL,
    travel_time_floor_s     REAL NOT NULL,
    failure_reason          TEXT,
    solved_e                REAL,
    solved_n                REAL,
    solved_alt              REAL,
    solve_residual_m        REAL,
    solve_method            TEXT,
    solve_ambiguous_json    TEXT,
    solved_at               TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

-- Per-neighbour pull execution state for a tdoa_attempts row (milestone 2).
-- One row per node that planning selected, PLUS (as of milestone 3) one row
-- for the origin node itself (status='origin') and one for every node
-- resolved via reuse (status='reused_existing') — folding the origin into
-- this table too means milestone 4's corroborating-node count is a single
-- uniform query instead of origin/known/pulled being counted three
-- different ways. request_id is NULL when the pull could not even be
-- issued (e.g. node unreachable, broker down), or when status is 'origin'
-- or 'reused_existing' (audio_event_id is set instead — the WAV already
-- existed, nothing was requested over the air for this row).
--
-- arrival_us (milestone 3): the absolute node-clock microsecond timestamp
-- of the acoustic event's onset in this node's WAV, derived by running the
-- attempt's onset_detection_method against the file named by
-- audio_events.filename. NULL until correlation succeeds. status reflects
-- the outcome: 'arrived' (arrival_us set), 'onset_failed' (WAV present but
-- no usable transient found, or filename missing — error holds why),
-- 'push_failed' (added 2026-07-13: the node acked the pull but explicitly
-- reported it couldn't deliver the audio — uplink busy/transport failure,
-- or the window had already aged out of its PSRAM ring buffer — see
-- routes.py audio_ack(). Distinct from 'request_failed', which means the
-- hub never got the pull out in the first place.)
CREATE TABLE IF NOT EXISTS tdoa_attempt_nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id  INTEGER NOT NULL REFERENCES tdoa_attempts(id) ON DELETE CASCADE,
    node_id     TEXT NOT NULL,
    request_id  INTEGER,
    status      TEXT NOT NULL,
    arrival_us  REAL,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

# Values for `approval_status`. New nodes land as PENDING — being seen on the
# network (self-registration or a manual add) is not enough to admit a node
# into the active set; an operator decision is required. REJECTED
# nodes are retained (not deleted) so a rejection can be reversed later
# without the node needing to be re-discovered from scratch.
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"

# trigger_events is diagnostic (near-miss/fire) data, not the permanent
# record — detections/audio_events are. It reached 18M+ rows / 2.3GB with no
# retention policy at all (see idx_trigger_events_t_us migration above),
# which was slow enough to trip upstream request timeouts. Now that
# trigger_event_rollups preserves the long-term shape of this data (rate,
# ratio range per node per minute — see rollup_trigger_events()), raw rows
# only need to survive long enough to be useful for live/recent debugging,
# not for historical analysis.
#
# Raw row count scales with (rate per node) x (node count) x (retention
# window) — node count is the one of those three that's about to grow (at
# least 5 nodes planned, likely more), so this is set in hours rather than
# days specifically so the total row budget can be dialed back down as more
# nodes come online, rather than the table quietly creeping back toward the
# scale that caused this whole investigation. Not read from config,
# deliberately, to avoid a footgun where an existing deployment's
# soundhub.conf doesn't define it.
TRIGGER_EVENTS_RETENTION_HOURS = 6

# Rollup bucket width for trigger_event_rollups — see rollup_trigger_events().
TRIGGER_EVENT_ROLLUP_BUCKET_US = 60_000_000  # 1 minute

# Only roll up buckets whose window closed at least this long ago, so a
# bucket that's still receiving inserts from the current/recent poll cycles
# is never finalized (and thus INSERT OR REPLACE'd as "done") prematurely.
# Comfortably larger than STATUS_POLL_INTERVAL_S (5s) plus the ring buffer's
# multi-poll overlap window.
TRIGGER_EVENT_ROLLUP_SAFETY_MARGIN_US = 120_000_000  # 2 minutes

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
    "onset_threshold_factor": 8.0,
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

        # Migration: mDNS discovery was removed 2026-07-12 (see project memory
        # `project-mdns-to-dns-migration`) — 'mdns' is no longer a valid
        # discovery_method (NodeView's Literal type in models.py no longer
        # includes it, so leaving old rows as 'mdns' would 500 on any endpoint
        # that serializes them). Every node in this fleet self-registers on
        # boot (has done since 2026-06-23), so relabel to 'self_registered'
        # rather than the more noncommittal 'manual' — it reflects what these
        # nodes actually do today, not just their original discovery history.
        await conn.execute(
            "UPDATE nodes SET discovery_method = 'self_registered' "
            "WHERE discovery_method = 'mdns'"
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

        # Migration: drop surveyed_lat/lon/alt columns — replaced entirely by
        # the hub-side GPS EMA (registry.get_gps_ema) as of the GPS telemetry
        # simplification (see GPS-TELEMETRY-SIMPLIFICATION-PROPOSAL.md in
        # sound-capture-node). These were only ever a transient alternative
        # input for the origin-from-node back-projection, not an independent
        # ongoing fact worth persisting — actually removed, not just
        # deprecated, to avoid a stale value ever disagreeing with the live
        # EMA. Requires SQLite >=3.35 (2021) for DROP COLUMN; guarded so an
        # older runtime just logs and leaves the (harmless, unused) columns
        # in place rather than failing startup.
        cursor = await conn.execute("PRAGMA table_info(node_positions)")
        pos_columns = {row[1] for row in await cursor.fetchall()}
        for col in ("surveyed_lat", "surveyed_lon", "surveyed_alt"):
            if col in pos_columns:
                try:
                    await conn.execute(f"ALTER TABLE node_positions DROP COLUMN {col}")
                except aiosqlite.OperationalError:
                    log.warning(
                        "Could not drop node_positions.%s (SQLite <3.35?) — "
                        "leaving it in place, unused", col,
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

        # Migration: add audio_event_id to tdoa_attempt_nodes if it doesn't
        # exist yet — lets a per-node row point directly at an existing
        # audio_events row it reused instead of issuing a fresh pull, for
        # detection-coalescing (see routes.py _plan_tdoa_attempt_inner).
        cursor = await conn.execute("PRAGMA table_info(tdoa_attempt_nodes)")
        tdoa_attempt_node_columns = {row[1] for row in await cursor.fetchall()}
        if "audio_event_id" not in tdoa_attempt_node_columns:
            await conn.execute(
                "ALTER TABLE tdoa_attempt_nodes ADD COLUMN audio_event_id INTEGER "
                "REFERENCES audio_events(id)"
            )

        # Migration: add arrival_us to tdoa_attempt_nodes (milestone 3 —
        # correlate arrivals) if it doesn't exist yet.
        if "arrival_us" not in tdoa_attempt_node_columns:
            await conn.execute(
                "ALTER TABLE tdoa_attempt_nodes ADD COLUMN arrival_us REAL"
            )

        # Migration: add filename to audio_events (milestone 3 — needed to
        # re-open a node's WAV for onset detection after the fact, e.g. a
        # 'reused_existing' corroborator's audio that arrived long before
        # the attempt that ends up using it) if it doesn't exist yet.
        if "filename" not in audio_event_columns:
            await conn.execute("ALTER TABLE audio_events ADD COLUMN filename TEXT")

        # Migration: add solve-result columns to tdoa_attempts (milestone 4)
        # if they don't exist yet.
        cursor = await conn.execute("PRAGMA table_info(tdoa_attempts)")
        tdoa_attempt_columns = {row[1] for row in await cursor.fetchall()}
        for col, coltype in (
            ("solved_e", "REAL"), ("solved_n", "REAL"), ("solved_alt", "REAL"),
            ("solve_residual_m", "REAL"), ("solve_method", "TEXT"),
            ("solve_ambiguous_json", "TEXT"), ("solved_at", "TEXT"),
        ):
            if col not in tdoa_attempt_columns:
                await conn.execute(f"ALTER TABLE tdoa_attempts ADD COLUMN {col} {coltype}")

        # Migration: add onset_threshold_factor to species_tdoa_params (per-
        # species onset tuning) if it doesn't exist yet.
        cursor = await conn.execute("PRAGMA table_info(species_tdoa_params)")
        species_param_columns = {row[1] for row in await cursor.fetchall()}
        if "onset_threshold_factor" not in species_param_columns:
            await conn.execute(
                "ALTER TABLE species_tdoa_params "
                "ADD COLUMN onset_threshold_factor REAL NOT NULL DEFAULT 8.0"
            )

        # Migration: add onset_threshold_factor/freq_band_low_hz/
        # freq_band_high_hz snapshot columns to tdoa_attempts (same per-
        # species onset tuning, copied at plan time like correlation_method/
        # onset_detection_method above) if they don't exist yet.
        if "onset_threshold_factor" not in tdoa_attempt_columns:
            await conn.execute(
                "ALTER TABLE tdoa_attempts "
                "ADD COLUMN onset_threshold_factor REAL NOT NULL DEFAULT 8.0"
            )
        if "freq_band_low_hz" not in tdoa_attempt_columns:
            await conn.execute("ALTER TABLE tdoa_attempts ADD COLUMN freq_band_low_hz REAL")
        if "freq_band_high_hz" not in tdoa_attempt_columns:
            await conn.execute("ALTER TABLE tdoa_attempts ADD COLUMN freq_band_high_hz REAL")

        # Migration: dedupe tdoa_attempt_nodes before adding the UNIQUE index
        # below (found 2026-07-13 — the straggler-fold-in path in routes.py's
        # _plan_tdoa_attempt_inner could insert a second 'reused_existing' row
        # for a node that already had a 'requested'/'origin' row on the same
        # attempt, from the concurrent neighbour-pull fan-out. Two rows for
        # one physical node meant _maybe_solve_tdoa_attempt_inner could feed
        # the same sensor position into the solver twice with two different
        # timestamps — a real correctness bug, not just a duplicate row.
        # CREATE UNIQUE INDEX below would fail outright against any
        # already-existing duplicate, so existing dupes must be resolved
        # first. Keeps, per (attempt_id, node_id): an 'arrived' row over
        # anything else (it carries a real correlated arrival_us — never
        # discard real data), else any non-terminal-failure row over a
        # 'request_failed'/'onset_failed' one, else the most recently
        # inserted (highest id). One-time cleanup — insert_tdoa_attempt_node()
        # below is now dedup-safe going forward, so this should never find
        # anything to do again after the first run against an existing DB.
        await conn.execute(
            """DELETE FROM tdoa_attempt_nodes
               WHERE id IN (
                   SELECT id FROM (
                       SELECT id,
                              ROW_NUMBER() OVER (
                                  PARTITION BY attempt_id, node_id
                                  ORDER BY
                                      CASE WHEN status = 'arrived' THEN 0
                                           WHEN status NOT IN
                                               ('request_failed', 'onset_failed')
                                           THEN 1
                                           ELSE 2 END,
                                      id DESC
                              ) AS rn
                       FROM tdoa_attempt_nodes
                   )
                   WHERE rn > 1
               )"""
        )
        try:
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tdoa_attempt_nodes_attempt_node "
                "ON tdoa_attempt_nodes(attempt_id, node_id)"
            )
        except aiosqlite.OperationalError:
            log.warning(
                "Could not create idx_tdoa_attempt_nodes_attempt_node — "
                "duplicate (attempt_id, node_id) rows may still exist despite "
                "the cleanup above; insert_tdoa_attempt_node()'s INSERT OR "
                "IGNORE still prevents new duplicates going forward even "
                "without this index, just without a DB-level guarantee."
            )

        # Index backing find_covering_audio_event's per-node window lookup —
        # same rationale as idx_trigger_events_t_us below: audio_events grows
        # large and this query runs on every TDOA planning pass.
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audio_events_node_window "
            "ON audio_events(node_id, t_start_us, t_end_us)"
        )

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

        # Seed audio_cleanup_settings with defaults on first run — INSERT OR
        # IGNORE is a no-op once the row exists, so this is safe to run on
        # every startup. Keeps get_audio_cleanup_settings() simple (row is
        # always present, no lazy-create branch needed at the route layer).
        await conn.execute(
            """INSERT OR IGNORE INTO audio_cleanup_settings
               (id, retention_hours, max_size_bytes, updated_at)
               VALUES (1, ?, ?, ?)""",
            (AUDIO_RETENTION_HOURS_DEFAULT, AUDIO_MAX_SIZE_BYTES_DEFAULT,
             datetime.now(timezone.utc).isoformat()),
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
    updated_at: str,
) -> None:
    """Insert or replace the hub-stored position for a node."""
    async with connect() as conn:
        await conn.execute(
            """INSERT OR REPLACE INTO node_positions
               (node_id, pos_e, pos_n, pos_alt, pos_status,
                is_origin, origin_lat, origin_lon, origin_alt, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (node_id, pos_e, pos_n, pos_alt, pos_status,
             1 if is_origin else 0,
             origin_lat, origin_lon, origin_alt, updated_at),
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
# audio_cleanup_settings CRUD  (governs audio_cleanup.py's periodic sweep)
# ---------------------------------------------------------------------------

# Used both to seed the row in init_db() and as the values a fresh install
# starts with. Minimums (3h / 1GB) live in models.py's Field constraints,
# not here — see audio_cleanup_settings table comment above.
AUDIO_RETENTION_HOURS_DEFAULT = 24.0
AUDIO_MAX_SIZE_BYTES_DEFAULT = 5 * 1024 ** 3  # 5 GB


async def get_audio_cleanup_settings() -> dict:
    """Return the audio cleanup settings. Always returns a row — init_db()
    seeds it on first run, and there is no delete endpoint for this
    singleton (unlike species_tdoa_params rows)."""
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM audio_cleanup_settings WHERE id = 1")
        row = await cursor.fetchone()
        return dict(row)


async def set_audio_cleanup_settings(
    *, retention_hours: float, max_size_bytes: int, updated_at: str,
) -> None:
    """Insert or replace the audio cleanup settings (always row id=1)."""
    async with connect() as conn:
        await conn.execute(
            """INSERT OR REPLACE INTO audio_cleanup_settings
               (id, retention_hours, max_size_bytes, updated_at)
               VALUES (1, ?, ?, ?)""",
            (retention_hours, max_size_bytes, updated_at),
        )
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
    filename: str | None = None,
) -> int:
    """Record one push received at POST /api/audio/push, regardless of outcome.

    analysis_status is one of 'analyzed' | 'skipped_not_ready' | 'error'.
    t_start_us/t_end_us are the node-clock capture window — present for
    triggered (node-initiated) pushes, NULL for hub-requested pulls (the hub
    already has those from its own request).

    filename is the WAV's name under the audio/ dir (routes.py audio_push()'s
    fname) — lets later code (TDOA correlation, milestone 3) re-open this
    push's audio without re-deriving the requestId/nodeId/srcMac naming
    scheme. Optional so existing call sites can be migrated incrementally,
    though as of milestone 3 all of audio_push()'s call sites pass it.

    Returns the inserted row's id — used by the TDOA orchestration hook in
    routes.py to link a tdoa_attempts row back to the audio_event that
    triggered it.
    """
    async with connect() as conn:
        cursor = await conn.execute(
            """INSERT INTO audio_events
               (node_id, triggered, received_at, bytes, analysis_status,
                detection_count, top_confidence, top_species, t_start_us,
                t_end_us, filename)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (node_id, 1 if triggered else 0, received_at, bytes_, analysis_status,
             detection_count, top_confidence, top_species, t_start_us, t_end_us,
             filename),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_audio_event(audio_event_id: int) -> dict | None:
    """Return one audio_events row by id, or None. Used by TDOA correlation
    (routes.py _correlate_attempt_node) to look up the filename/t_start_us
    needed to re-open a 'reused_existing' or origin node's WAV — those rows
    only carry audio_event_id, not the file details themselves."""
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM audio_events WHERE id = ?", (audio_event_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


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


async def prune_trigger_events(retention_hours: int = TRIGGER_EVENTS_RETENTION_HOURS) -> int:
    """Delete trigger_events rows older than retention_hours. Returns the
    number of rows deleted.

    t_us is node-clock UTC microseconds (not a hub wall-clock timestamp), so
    the cutoff is computed the same way for a correct comparison. Intended
    to be called periodically from the poller (see poller.run()) rather than
    on every poll tick — an hour-granularity retention window doesn't need
    sub-minute precision, and this table is large enough that a tighter
    cadence would just be unnecessary write churn.

    The very first call against an already-large, never-pruned table can
    delete a large fraction of it in one transaction — consider running an
    equivalent one-off `DELETE FROM trigger_events WHERE t_us < ...` via the
    sqlite3 CLI at a quiet moment first, same as the index migration above,
    rather than letting it happen cold on a live server.
    """
    cutoff_us = int(
        (datetime.now(timezone.utc) - timedelta(hours=retention_hours)).timestamp()
        * 1_000_000
    )
    async with connect() as conn:
        cursor = await conn.execute(
            "DELETE FROM trigger_events WHERE t_us < ?", (cutoff_us,)
        )
        await conn.commit()
        return cursor.rowcount if cursor.rowcount is not None and cursor.rowcount > 0 else 0


async def rollup_trigger_events() -> int:
    """Aggregate settled trigger_events rows into 1-minute-bucket summary
    rows in trigger_event_rollups, one bucket per (node_id, bucket_start_us).
    Returns the number of bucket rows written (inserted or updated).

    Reads from the already-persisted raw table rather than trying to
    deduplicate the node's live, overlapping ring-buffer dump itself —
    trigger_events' own UNIQUE(node_id, t_us) + INSERT OR IGNORE already
    solved that problem correctly, so this just aggregates already-
    deduplicated rows via GROUP BY.

    Per node, only rows since that node's last-rolled-up bucket are
    re-aggregated (tracked via MAX(bucket_start_us) already in
    trigger_event_rollups — no separate watermark table needed), so a run
    only ever processes the last rollup interval's worth of new rows rather
    than rescanning the whole raw retention window every time.

    INSERT OR REPLACE on (node_id, bucket_start_us) makes each bucket write
    idempotent — safe to rerun, e.g. if this is invoked more often than the
    configured interval for any reason.
    """
    cutoff_bucket_us = (
        (int(datetime.now(timezone.utc).timestamp() * 1_000_000)
         - TRIGGER_EVENT_ROLLUP_SAFETY_MARGIN_US)
        // TRIGGER_EVENT_ROLLUP_BUCKET_US
    ) * TRIGGER_EVENT_ROLLUP_BUCKET_US
    now_iso = datetime.now(timezone.utc).isoformat()

    total_buckets = 0
    async with connect() as conn:
        cursor = await conn.execute("SELECT DISTINCT node_id FROM trigger_events")
        node_ids = [row[0] for row in await cursor.fetchall()]

        for node_id in node_ids:
            cursor = await conn.execute(
                "SELECT MAX(bucket_start_us) FROM trigger_event_rollups WHERE node_id = ?",
                (node_id,),
            )
            (last_bucket,) = await cursor.fetchone()
            start_us = 0 if last_bucket is None else last_bucket + TRIGGER_EVENT_ROLLUP_BUCKET_US

            cursor = await conn.execute(
                """SELECT
                       (t_us / ?) * ? AS bucket_start_us,
                       COUNT(*), SUM(fired),
                       MIN(energy_ratio), AVG(energy_ratio), MAX(energy_ratio),
                       MIN(flux_ratio), AVG(flux_ratio), MAX(flux_ratio)
                   FROM trigger_events
                   WHERE node_id = ? AND t_us >= ? AND t_us < ?
                   GROUP BY bucket_start_us""",
                (TRIGGER_EVENT_ROLLUP_BUCKET_US, TRIGGER_EVENT_ROLLUP_BUCKET_US,
                 node_id, start_us, cutoff_bucket_us),
            )
            rows = await cursor.fetchall()
            if not rows:
                continue

            await conn.executemany(
                """INSERT OR REPLACE INTO trigger_event_rollups
                   (node_id, bucket_start_us, entry_count, fired_count,
                    energy_ratio_min, energy_ratio_avg, energy_ratio_max,
                    flux_ratio_min, flux_ratio_avg, flux_ratio_max, rolled_up_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (node_id, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], now_iso)
                    for r in rows
                ],
            )
            total_buckets += len(rows)

        await conn.commit()
    return total_buckets


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


async def trigger_ratio_histogram(
    node_id: str | None, since_us: int, until_us: int,
    bucket_width: float = 1.0, max_ratio: float = 20.0,
) -> dict:
    """Bucket energy_ratio and flux_ratio from trigger_events over [since_us,
    until_us] into fixed-width bins, with a per-bucket fired-row count.

    Answers "where does the near-miss distribution sit relative to the fire
    threshold" — the question the raw per-block detail table turned out not
    to answer well (a sustained call floods it with near-identical rows,
    burying the shape). Necessarily scoped to raw trigger_events, so it can
    only see back as far as TRIGGER_EVENTS_RETENTION_HOURS — unlike
    trigger_event_rollups, which survives indefinitely but only keeps
    min/avg/max per minute, not enough to reconstruct a distribution.

    Values are clamped to max_ratio before bucketing (SQLite's multi-arg
    MIN() is a scalar function, not the aggregate — safe to mix with the
    aggregate COUNT/SUM in the same query) so a rare very-high-ratio fire
    doesn't stretch the bucket range and squash the near-threshold detail
    that actually matters for a retuning decision. That top bucket is an
    open-ended "≥ max_ratio" overflow, not a true bin.

    node_id=None combines all nodes into one histogram — a reasonable first
    look, but per-node is more meaningful for an actual threshold decision:
    nodes have shown real SNR/hardware-driven differences in observed ratios
    for the same physical event (e.g. 574 vs 236 energy_ratio, see
    project history), which a combined histogram would blur together.

    Returns {"energy": [{"bucket": int, "count": int, "fired_count": int}, ...],
             "flux": [...]}, one entry per non-empty bucket, ordered by
    bucket index. `bucket` is a bin *index* (multiply by bucket_width at the
    call site to get the bin's lower edge in ratio units — see
    trigger_diag_histogram() in routes.py, which does exactly that).
    """
    where = ["t_us >= ?", "t_us <= ?"]
    params: list = [since_us, until_us]
    if node_id:
        where.append("node_id = ?")
        params.append(node_id)
    where_clause = " AND ".join(where)

    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        result: dict = {}
        for key, column in (("energy", "energy_ratio"), ("flux", "flux_ratio")):
            cursor = await conn.execute(
                f"""SELECT
                        CAST(MIN({column}, ?) / ? AS INTEGER) AS bucket,
                        COUNT(*) AS count,
                        SUM(fired) AS fired_count
                    FROM trigger_events
                    WHERE {where_clause}
                    GROUP BY bucket
                    ORDER BY bucket""",
                [max_ratio, bucket_width, *params],
            )
            rows = await cursor.fetchall()
            result[key] = [dict(row) for row in rows]
        return result


async def list_trigger_rollups(
    node_id: str | None = None,
    from_us: int | None = None,
    to_us: int | None = None,
    limit: int = 50_000,
) -> list[dict]:
    """Return per-minute trigger_events rollups, oldest first.

    Unlike raw trigger_events (pruned after TRIGGER_EVENTS_RETENTION_HOURS),
    rollups are never pruned — this is the only way to see trigger activity
    (near-miss + fire rate over time) beyond the last few hours. Built to
    answer "does this near-zero-ratio fire cluster with a plausible burst of
    real activity, or sit isolated with nothing around it" — the histogram
    can't answer that, since it collapses time away entirely.

    limit defensively caps an unbounded (no from_us/to_us) query — at one
    row per node per minute this is normally small, but an unbounded range
    on a long-running multi-node deployment could still be large.
    """
    where = ["1=1"]
    params: list = []
    if node_id:
        where.append("node_id = ?")
        params.append(node_id)
    if from_us is not None:
        where.append("bucket_start_us >= ?")
        params.append(from_us)
    if to_us is not None:
        where.append("bucket_start_us <= ?")
        params.append(to_us)
    params.append(limit)

    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            f"""SELECT * FROM trigger_event_rollups
                WHERE {' AND '.join(where)}
                ORDER BY bucket_start_us ASC LIMIT ?""",
            params,
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
    onset_threshold_factor: float,
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
                onset_threshold_factor, freq_band_low_hz, freq_band_high_hz,
                pull_window_s, window_margin_pre_ms, window_margin_post_ms,
                min_corroborating_nodes, notes, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (species_key, 1 if enabled else 0, correlation_method,
             onset_detection_method, onset_threshold_factor, freq_band_low_hz,
             freq_band_high_hz, pull_window_s, window_margin_pre_ms,
             window_margin_post_ms, min_corroborating_nodes, notes, updated_at),
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
    onset_threshold_factor: float,
    freq_band_low_hz: float | None,
    freq_band_high_hz: float | None,
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
                onset_detection_method, onset_threshold_factor,
                freq_band_low_hz, freq_band_high_hz, travel_time_floor_s,
                failure_reason, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (audio_event_id, origin_node_id, species_key, 1 if used_default else 0,
             status, t_start_us, t_end_us, planned_node_ids,
             min_corroborating_nodes, correlation_method, onset_detection_method,
             onset_threshold_factor, freq_band_low_hz, freq_band_high_hz,
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


async def resolve_tdoa_attempt_ids_in_range(
    from_ts: str | None, to_ts: str | None,
) -> list[int]:
    """Return tdoa_attempts.id values with created_at in [from_ts, to_ts]
    (either bound may be omitted — the route layer enforces that at least
    one is given; this function itself has no opinion on that). Feeds
    delete_tdoa_attempts() for the bulk "delete attempts in this range"
    admin action."""
    clauses: list[str] = []
    params: list[str] = []
    if from_ts:
        clauses.append("created_at >= ?")
        params.append(from_ts)
    if to_ts:
        clauses.append("created_at <= ?")
        params.append(to_ts)
    where = " AND ".join(clauses) if clauses else "1=1"
    async with connect() as conn:
        cursor = await conn.execute(
            f"SELECT id FROM tdoa_attempts WHERE {where}", params,
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]


async def delete_tdoa_attempts(attempt_ids: list[int]) -> dict:
    """Delete the given tdoa_attempts rows and their tdoa_attempt_nodes,
    used by the admin "clear meaningless TDOA results" action (e.g. bench
    testing with nodes sitting together but configured with their intended
    field positions, producing solved-but-nonsensical coordinates).

    Also deletes any audio_events row that exists purely for TDOA
    corroboration (analysis_status == 'skipped_birdnet_tdoa_pull' — see
    _save_direct_pull_audio in routes.py) and, after this deletion, is no
    longer referenced by any remaining tdoa_attempt_nodes row from a
    *different* attempt (a corroboration WAV can be reused across attempts
    via find_covering_audio_event's 'reused_existing' path, so it isn't
    automatically safe to drop just because the attempt that first pulled
    it is being deleted).

    Deliberately does NOT touch the audio_events/detections rows an origin
    node's own trigger created (analysis_status == 'analyzed' etc.), even
    though they're linked to a deleted attempt via a status='origin' row —
    those are real BirdNET detection history, independent of whether the
    TDOA solve made sense, and must survive this cleanup.

    Returns {"attempts_deleted": int, "filenames_deleted": list[str]} — the
    caller (routes.py) is responsible for actually unlinking those
    filenames from disk; this function only touches the database.
    """
    if not attempt_ids:
        return {"attempts_deleted": 0, "filenames_deleted": []}

    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        placeholders = ",".join("?" for _ in attempt_ids)

        cursor = await conn.execute(
            f"""SELECT DISTINCT tan.audio_event_id AS id
                FROM tdoa_attempt_nodes tan
                JOIN audio_events ae ON ae.id = tan.audio_event_id
                WHERE tan.attempt_id IN ({placeholders})
                  AND ae.analysis_status = 'skipped_birdnet_tdoa_pull'""",
            attempt_ids,
        )
        candidate_ids = [row["id"] for row in await cursor.fetchall()]

        await conn.execute(
            f"DELETE FROM tdoa_attempt_nodes WHERE attempt_id IN ({placeholders})",
            attempt_ids,
        )
        cursor = await conn.execute(
            f"DELETE FROM tdoa_attempts WHERE id IN ({placeholders})", attempt_ids,
        )
        attempts_deleted = cursor.rowcount

        filenames_deleted: list[str] = []
        for audio_event_id in candidate_ids:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM tdoa_attempt_nodes WHERE audio_event_id = ?",
                (audio_event_id,),
            )
            (still_referenced,) = await cursor.fetchone()
            if still_referenced:
                continue
            cursor = await conn.execute(
                "SELECT filename FROM audio_events WHERE id = ?", (audio_event_id,),
            )
            row = await cursor.fetchone()
            await conn.execute(
                "DELETE FROM audio_events WHERE id = ?", (audio_event_id,),
            )
            if row and row["filename"]:
                filenames_deleted.append(row["filename"])

        await conn.commit()

    return {"attempts_deleted": attempts_deleted, "filenames_deleted": filenames_deleted}


async def get_tdoa_attempt(attempt_id: int) -> dict | None:
    """Return one tdoa_attempts row by id, or None. Used by milestone 4's
    solve-readiness check (routes.py _maybe_solve_tdoa_attempt_inner) to
    re-read min_corroborating_nodes/species_key/status after a node's
    arrival has just been recorded."""
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM tdoa_attempts WHERE id = ?", (attempt_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


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


async def persist_tdoa_solution(
    attempt_id: int,
    *,
    e: float,
    n: float,
    alt: float,
    residual_m: float,
    method: str,
    ambiguous_root_json: str | None = None,
) -> None:
    """Milestone 4: write a successful tdoa_solver.solve() result back to
    the attempt row and advance status to 'solved'.

    ambiguous_root_json is a pre-serialized JSON array `[x, y, z]` for a
    4-node (quadratic) solve's mirror root, or None for a 5+-node
    (least-squares) solve where solve() reports no ambiguity — see
    tdoa_solver.SolveResult.ambiguous_root. No hint_point is wired into the
    automatic solve as of milestone 4, so a 4-node solve's mirror root is
    stored for manual review, not auto-resolved.
    """
    now = datetime.now(timezone.utc).isoformat()
    async with connect() as conn:
        await conn.execute(
            """UPDATE tdoa_attempts
               SET status = 'solved', solved_e = ?, solved_n = ?,
                   solved_alt = ?, solve_residual_m = ?, solve_method = ?,
                   solve_ambiguous_json = ?, solved_at = ?, updated_at = ?
               WHERE id = ?""",
            (e, n, alt, residual_m, method, ambiguous_root_json, now, now,
             attempt_id),
        )
        await conn.commit()


async def insert_tdoa_attempt_node(
    *,
    attempt_id: int,
    node_id: str,
    request_id: int | None,
    status: str,
    error: str | None = None,
    audio_event_id: int | None = None,
) -> int:
    """Insert one per-neighbour pull execution record against a tdoa_attempts
    row (milestone 2). Returns the row's id — either newly inserted, or the
    id of an already-existing row for this exact (attempt_id, node_id) pair.

    Dedup-safe (added 2026-07-13): a node can only ever have one row per
    attempt (idx_tdoa_attempt_nodes_attempt_node, a UNIQUE index — see
    init_db()'s migration). This matters because the same node can reach
    this function twice for one attempt: once from the concurrent
    neighbour-pull fan-out (_pull_or_reuse_one), and again if it also
    self-triggers slightly late and gets folded in as a straggler
    (_plan_tdoa_attempt_inner's find_open_tdoa_attempt branch). Without this
    guard, both inserts would succeed, giving one physical node two rows —
    and if both ever reached status='arrived', the solver would be fed the
    same sensor position twice with two different timestamps. INSERT OR
    IGNORE silently drops the second attempt instead; the first row (whichever
    arrived first) wins and callers get its id back rather than creating a
    duplicate. This means a losing caller's status/audio_event_id/error are
    NOT applied — acceptable here since every current call site either
    doesn't touch the row again (the straggler branch) or immediately
    overwrites via update_tdoa_attempt_node_result using its own data
    regardless of which row id it got back (_pull_or_reuse_one's
    reused_existing branch) — see routes.py.

    request_id is None when the pull could never be issued (error holds why)
    or when status='reused_existing' — audio_event_id is set instead,
    pointing at the already-known WAV (either the node's own detection that
    put it in the same debounce cluster as the origin, or a prior push/pull
    whose window already covered what this attempt needed). See
    detection-coalescing notes in routes.py _plan_tdoa_attempt_inner."""
    now = datetime.now(timezone.utc).isoformat()
    async with connect() as conn:
        cursor = await conn.execute(
            """INSERT OR IGNORE INTO tdoa_attempt_nodes
               (attempt_id, node_id, request_id, status, error,
                audio_event_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (attempt_id, node_id, request_id, status, error, audio_event_id, now, now),
        )
        await conn.commit()
        if cursor.rowcount:
            return cursor.lastrowid
        # Ignored — a row for this (attempt_id, node_id) pair already
        # existed. Fetch and return its id instead of a fresh one.
        cursor = await conn.execute(
            "SELECT id FROM tdoa_attempt_nodes WHERE attempt_id = ? AND node_id = ?",
            (attempt_id, node_id),
        )
        row = await cursor.fetchone()
        return row[0]


async def list_tdoa_attempt_nodes(attempt_id: int) -> list[dict]:
    """Return all per-neighbour pull records for one TDOA attempt, each with
    its linked audio_events.filename joined in (as `filename`) — lets a
    caller (GET /api/tdoa/attempts) point at the actual WAV on disk without
    a second round-trip per node. LEFT JOIN because audio_event_id is NULL
    for a 'requested'/'request_failed' row that hasn't correlated (or never
    will) yet — those rows correctly get filename=NULL, not excluded.
    """
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT tan.*, ae.filename AS filename
               FROM tdoa_attempt_nodes tan
               LEFT JOIN audio_events ae ON ae.id = tan.audio_event_id
               WHERE tan.attempt_id = ?
               ORDER BY tan.id""",
            (attempt_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def update_tdoa_attempt_node_result(
    node_row_id: int,
    *,
    status: str,
    arrival_us: float | None = None,
    error: str | None = None,
    audio_event_id: int | None = None,
) -> None:
    """Milestone 3: record one node's correlation outcome against its
    tdoa_attempt_nodes row — status='arrived' with arrival_us set on
    success, or 'onset_failed' with error set (no usable transient, or the
    WAV's filename was missing/unreadable). audio_event_id is passed through
    for the corroboration-push path, which doesn't have it yet at insert
    time (the audio_event is only created once the WAV lands — see
    insert_tdoa_attempt_node's 'requested' rows); left untouched (not
    overwritten with NULL) when not given, matching
    update_tdoa_attempt_status's failure_reason convention."""
    now = datetime.now(timezone.utc).isoformat()
    async with connect() as conn:
        if audio_event_id is not None:
            await conn.execute(
                """UPDATE tdoa_attempt_nodes
                   SET status = ?, arrival_us = ?, error = ?,
                       audio_event_id = ?, updated_at = ?
                   WHERE id = ?""",
                (status, arrival_us, error, audio_event_id, now, node_row_id),
            )
        else:
            await conn.execute(
                """UPDATE tdoa_attempt_nodes
                   SET status = ?, arrival_us = ?, error = ?, updated_at = ?
                   WHERE id = ?""",
                (status, arrival_us, error, now, node_row_id),
            )
        await conn.commit()


async def find_tdoa_attempt_node_by_request_id(request_id: int) -> dict | None:
    """Milestone 3: given the requestId an arriving corroboration push is
    tagged with, find the tdoa_attempt_nodes row it corresponds to, joined
    with the fields from its parent tdoa_attempts row that correlation
    needs (species_key, onset_detection_method, onset_threshold_factor,
    freq_band_low_hz/high_hz, min_corroborating_nodes).
    Returns None if no attempt ever issued this requestId (e.g. a manual
    /nodes/{id}/sample pull, purpose='manual')."""
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT tan.id AS node_row_id, tan.attempt_id, tan.node_id,
                      tan.status AS node_status,
                      ta.species_key, ta.onset_detection_method,
                      ta.onset_threshold_factor, ta.freq_band_low_hz,
                      ta.freq_band_high_hz, ta.min_corroborating_nodes
               FROM tdoa_attempt_nodes tan
               JOIN tdoa_attempts ta ON ta.id = tan.attempt_id
               WHERE tan.request_id = ?
               ORDER BY tan.id DESC LIMIT 1""",
            (request_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def find_covering_audio_event(
    node_id: str, t_start_us: int, t_end_us: int
) -> dict | None:
    """Return the most recent audio_events row for node_id whose stored
    capture window fully contains [t_start_us, t_end_us], or None.

    Used by TDOA planning (routes.py _plan_tdoa_attempt_inner) to avoid
    issuing a redundant pull to a node that already pushed (via its own
    trigger, or a prior pull for a different attempt) audio covering the
    window this attempt needs — see project_soundhub_tdoa_dedup design
    notes. Requires t_start_us/t_end_us to be non-NULL on the candidate row,
    which excludes older rows predating that column and any push where the
    node didn't report its actual capture window.
    """
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT * FROM audio_events
               WHERE node_id = ?
                 AND t_start_us IS NOT NULL AND t_end_us IS NOT NULL
                 AND t_start_us <= ? AND t_end_us >= ?
               ORDER BY id DESC LIMIT 1""",
            (node_id, t_start_us, t_end_us),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def find_open_tdoa_attempt(
    species_key: str, t_start_us: int, t_end_us: int
) -> dict | None:
    """Return the most recent in-flight tdoa_attempts row (status 'planned'
    or 'pulling') for species_key whose window overlaps [t_start_us,
    t_end_us], or None.

    Safety net for a detection that misses its own debounce/coalesce window
    (routes.py _register_detection_for_tdoa) — e.g. a slow relay hop lands
    it after the cluster already fired planning. Rather than spawning a
    second full attempt (and re-pulling neighbours the first attempt is
    already pulling), the caller attaches the straggler to this attempt
    instead. Overlap, not containment, is intentional: the straggler's own
    window need not be identical to the existing attempt's padded pull
    window, only to describe the same underlying acoustic event.
    """
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT * FROM tdoa_attempts
               WHERE species_key = ?
                 AND status IN ('planned', 'pulling')
                 AND t_start_us <= ? AND t_end_us >= ?
               ORDER BY id DESC LIMIT 1""",
            (species_key, t_end_us, t_start_us),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
