"""Pydantic schemas for the API."""
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AckStatus(str, Enum):
    """Maps to AckStatus enum in EspNowMessages.hpp."""
    ACK         = "ack"
    DONE        = "done"
    UNAVAILABLE = "unavailable"
    ERROR       = "error"


class AudioAckBody(BaseModel):
    """Body for POST /api/audio/ack — broker-forwarded node ack."""
    request_id: int       = Field(alias="requestId")
    status: AckStatus
    src_mac: str          = Field(alias="srcMac", description="Wi-Fi STA MAC of the responding node")

    model_config = ConfigDict(populate_by_name=True)


# Matches AudioStore::kHubPullMaxSeconds in sound-capture-node's firmware —
# the capacity of the pre-allocated hub-pull scratch slot (2026-07-09, added
# after a node-172 incident traced to per-call PSRAM allocation under
# fragmentation; see AudioStore.hpp for the full rationale). The automatic
# species-TDOA pull (_plan_tdoa_attempt in routes.py) never asks for more
# than ~8-9s in practice, so this only really constrains this manual path.
# Enforced hub-side so an oversized manual request fails fast with a clear
# 422 here rather than a silent node-side clip or an UNAVAILABLE. A window
# wider than this should be requested as multiple sequential pulls instead
# of raising the cap — see the sizing discussion in project chat history.
MAX_MANUAL_PULL_US = 10_000_000  # 10s


class AudioSampleRequest(BaseModel):
    """Body for POST /api/nodes/{id}/sample — trigger an audio pull from a node."""
    t_start_us: int = Field(alias="tStartUs", description="UTC µs — inclusive segment start")
    t_end_us:   int = Field(alias="tEndUs",   description="UTC µs — exclusive segment end")

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def _validate_window(self) -> "AudioSampleRequest":
        if self.t_end_us <= self.t_start_us:
            raise ValueError("tEndUs must be greater than tStartUs")
        duration_us = self.t_end_us - self.t_start_us
        if duration_us > MAX_MANUAL_PULL_US:
            raise ValueError(
                f"requested window ({duration_us / 1e6:.1f}s) exceeds the "
                f"{MAX_MANUAL_PULL_US / 1e6:.0f}s node-side scratch-slot cap — "
                "split into multiple sequential pulls instead"
            )
        return self


class ManualNodeRequest(BaseModel):
    """Body for POST /api/nodes/manual — fallback discovery path."""
    host: str  # hostname or bare IP; we hit http://<host>/app/api/status to validate


class NodeRegisterRequest(BaseModel):
    """Body for POST /api/nodes/register — node self-registration on boot.

    heap_free_bytes/heap_min_free_bytes are optional so older firmware
    builds that don't send them still register fine.
    """
    hostname: str
    mac: str
    heap_free_bytes: Optional[int] = Field(default=None, alias="heapFreeBytes")
    heap_min_free_bytes: Optional[int] = Field(default=None, alias="heapMinFreeBytes")
    https_active_sockets: Optional[int] = Field(default=None, alias="httpsActiveSockets")
    https_max_sockets: Optional[int] = Field(default=None, alias="httpsMaxSockets")

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# TDOA solver request / response
# ---------------------------------------------------------------------------

class NodeTimestamp(BaseModel):
    """One node's GPS-disciplined arrival timestamp for a sound event."""
    node_id: str = Field(alias="nodeId")
    timestamp_us: float = Field(alias="timestampUs", description="Absolute GPS timestamp in microseconds")

    model_config = ConfigDict(populate_by_name=True)


class TdoaRequest(BaseModel):
    """Body for POST /api/tdoa/solve.

    Supply one timestamp per participating node (minimum 4). The hub looks up
    each node's position from its database; nodes without a stored position are
    rejected with 422.

    hint_point selects the physically meaningful root when the 4-node quadratic
    yields two solutions. Provide any point in the expected source halfspace —
    e.g. a point deep in the monitored forest. If omitted the solver falls back
    to a centroid heuristic, which is unreliable for distant sources.
    """
    timestamps: list[NodeTimestamp]
    hint_point: Optional[tuple[float, float, float]] = Field(
        default=None,
        alias="hintPoint",
        description="(E, N, Alt) metres — any point in the expected source halfspace",
    )
    speed_of_sound: float = Field(
        default=343.0,
        alias="speedOfSound",
        description="Speed of sound in m/s. Brisbane subtropical: ~343–348 m/s.",
    )

    model_config = ConfigDict(populate_by_name=True)


class TdoaResponse(BaseModel):
    """Result from POST /api/tdoa/solve."""
    # Source position in the array's local coordinate frame (metres from origin).
    x: float = Field(description="East offset from array origin (metres)")
    y: float = Field(description="North offset from array origin (metres)")
    z: float = Field(description="Altitude offset from array origin (metres)")
    residual_m: float = Field(alias="residualM", description="RMS range residual (metres)")
    method: str = Field(description="'quadratic' (4 nodes) or 'least_squares' (5+ nodes)")
    # The mirror root, always returned for 4-node solves so the caller can
    # inspect it or apply their own disambiguation logic.
    ambiguous_root: Optional[tuple[float, float, float]] = Field(
        default=None,
        alias="ambiguousRoot",
        description="Mirror root (E, N, Alt) if 4-node quadratic, else null",
    )

    model_config = ConfigDict(populate_by_name=True)


class TdoaAttemptNodeRecord(BaseModel):
    """One node's contribution to a tdoa_attempts row — returned nested
    inside TdoaAttemptRecord by GET /api/tdoa/attempts.

    status is one of 'requested' | 'request_failed' | 'reused_existing' |
    'origin' | 'arrived' | 'onset_failed' — see tdoa_attempt_nodes' schema
    comment in db.py for what each means at every stage of milestones 2-3.
    """
    id: int
    node_id: str = Field(alias="nodeId")
    request_id: Optional[int] = Field(default=None, alias="requestId")
    status: str
    arrival_us: Optional[float] = Field(default=None, alias="arrivalUs")
    error: Optional[str] = Field(default=None)
    audio_event_id: Optional[int] = Field(default=None, alias="audioEventId")
    filename: Optional[str] = Field(
        default=None,
        description="WAV filename under the hub's audio/ directory — joined "
                     "in from audio_events via audio_event_id (db.py's "
                     "list_tdoa_attempt_nodes). Null if this node hasn't "
                     "correlated (or never will) yet.",
    )
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")

    model_config = ConfigDict(populate_by_name=True)


class TdoaAttemptRecord(BaseModel):
    """One TDOA orchestration attempt (species_tdoa_pipeline design,
    sound-hub/DESIGN.md) — returned by GET /api/tdoa/attempts, newest first.

    status is one of 'planned' | 'pulling' | 'solved' | 'failed'. The
    solved_*/solve_* fields are only populated once status='solved';
    failure_reason only once status='failed'. solve_ambiguous_root is the
    4-node quadratic solve's mirror root (E, N, Alt) when present — see
    tdoa_solver.py; null for a 5+-node least-squares solve, which has no
    ambiguity, or for an unsolved/failed attempt.
    """
    id: int
    audio_event_id: Optional[int] = Field(default=None, alias="audioEventId")
    origin_node_id: Optional[str] = Field(default=None, alias="originNodeId")
    species_key: str = Field(alias="speciesKey")
    used_default: bool = Field(alias="usedDefault")
    status: str
    t_start_us: int = Field(alias="tStartUs")
    t_end_us: int = Field(alias="tEndUs")
    min_corroborating_nodes: int = Field(alias="minCorroboratingNodes")
    correlation_method: str = Field(alias="correlationMethod")
    onset_detection_method: str = Field(alias="onsetDetectionMethod")
    onset_threshold_factor: float = Field(alias="onsetThresholdFactor")
    freq_band_low_hz: Optional[float] = Field(default=None, alias="freqBandLowHz")
    freq_band_high_hz: Optional[float] = Field(default=None, alias="freqBandHighHz")
    travel_time_floor_s: float = Field(alias="travelTimeFloorS")
    failure_reason: Optional[str] = Field(default=None, alias="failureReason")
    solved_e: Optional[float] = Field(default=None, alias="solvedE")
    solved_n: Optional[float] = Field(default=None, alias="solvedN")
    solved_alt: Optional[float] = Field(default=None, alias="solvedAlt")
    solve_residual_m: Optional[float] = Field(default=None, alias="solveResidualM")
    solve_method: Optional[str] = Field(default=None, alias="solveMethod")
    solve_ambiguous_root: Optional[tuple[float, float, float]] = Field(
        default=None, alias="solveAmbiguousRoot",
    )
    solved_at: Optional[str] = Field(default=None, alias="solvedAt")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")
    nodes: list[TdoaAttemptNodeRecord] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


class NodeConfigRequest(BaseModel):
    """Body for POST /api/nodes/{id}/configure — proxied to the node's own
    POST /app/api/node-config (NodeConfigHandler.cpp — persists to NVS).

    Post Track A refactor: only hub-pushable fields that are genuinely
    node-resident remain here. Position data is no longer node-resident —
    it lives in the hub's node_positions table and is managed via
    PUT /api/nodes/{id}/position instead.

    All fields optional: the operator edits a subset and we forward only
    what they changed (exclude_unset), mirroring the node-side handler.
    """
    is_broker: Optional[bool] = Field(default=None, alias="isBroker")

    model_config = ConfigDict(populate_by_name=True)


class NodePosition(BaseModel):
    """Hub-stored position record for a node (node_positions table).

    This is the operator-managed position — distinct from the GPS telemetry
    the node reports live. The hub owns this data; the node never sees it.
    """
    pos_e: Optional[float] = Field(default=None, alias="posE")
    pos_n: Optional[float] = Field(default=None, alias="posN")
    pos_alt: Optional[float] = Field(default=None, alias="posAlt")
    pos_status: Literal["surveyed", "estimated"] = Field(
        default="estimated", alias="posStatus"
    )
    is_origin: bool = Field(default=False, alias="isOrigin")
    origin_lat: Optional[float] = Field(default=None, alias="originLat")
    origin_lon: Optional[float] = Field(default=None, alias="originLon")
    origin_alt: Optional[float] = Field(default=None, alias="originAlt")

    model_config = ConfigDict(populate_by_name=True)


class PositionFromEma(BaseModel):
    """Response for GET /api/nodes/{id}/position/from-ema.

    A preview, not a write: computed E/N/Alt offset for this node, derived
    by back-projecting its current hub-side GPS EMA (registry.get_gps_ema)
    through the array origin. Nothing is persisted until the operator
    applies it via PUT /api/nodes/{id}/position. emaLat/Lon/Alt and emaN are
    included so the UI can show what the preview was computed from.
    """
    pos_e: float = Field(alias="posE")
    pos_n: float = Field(alias="posN")
    pos_alt: float = Field(alias="posAlt")
    ema_lat: float = Field(alias="emaLat")
    ema_lon: float = Field(alias="emaLon")
    ema_alt: float = Field(alias="emaAlt")
    ema_n: int = Field(alias="emaN", description="Samples admitted into the EMA so far")

    model_config = ConfigDict(populate_by_name=True)


class ArrayOrigin(BaseModel):
    """Hub-level geographic datum for the node array (array_origin table).

    Set by POST /api/origin/set-from-node/{node_id} (back-projected from a
    node's live GPS EMA + surveyed array offset) or by PUT /api/origin
    (manual override).  Independent of any specific node.
    """
    lat: float
    lon: float
    alt_m: float = Field(alias="altM")
    set_from: Optional[str] = Field(
        default=None,
        alias="setFrom",
        description="node_id whose GPS centroid was used to establish this origin",
    )
    set_at: str = Field(alias="setAt")

    model_config = ConfigDict(populate_by_name=True)


class ArrayOriginManual(BaseModel):
    """Body for PUT /api/origin — manual lat/lon/alt override."""
    lat: float
    lon: float
    alt_m: float = Field(alias="altM")

    model_config = ConfigDict(populate_by_name=True)


class DetectionRecord(BaseModel):
    """One BirdNET detection row — returned by GET /api/detections."""
    id: int
    source: Optional[str] = None
    node_id: Optional[str] = Field(default=None, alias="nodeId")
    analyzed_at: str = Field(alias="analyzedAt")
    common_name: str = Field(alias="commonName")
    scientific_name: str = Field(alias="scientificName")
    confidence: float
    start_sec: Optional[float] = Field(default=None, alias="startSec")
    end_sec: Optional[float] = Field(default=None, alias="endSec")

    model_config = ConfigDict(populate_by_name=True)


class SpeciesSummary(BaseModel):
    """One species' aggregated detection stats — returned by
    GET /api/detections/species-summary."""
    common_name: str = Field(alias="commonName")
    scientific_name: str = Field(alias="scientificName")
    count: int
    last_seen: str = Field(alias="lastSeen")
    avg_confidence: float = Field(alias="avgConfidence")

    model_config = ConfigDict(populate_by_name=True)


class AudioEventRecord(BaseModel):
    """One audio push event — returned by GET /api/analytics/audio.

    Recorded for every push to POST /api/audio/push regardless of BirdNET
    outcome, unlike `detections` rows which only exist for pushes that
    cleared the persisted-detection confidence threshold.
    """
    id: int
    node_id: Optional[str] = Field(default=None, alias="nodeId")
    triggered: bool
    received_at: str = Field(alias="receivedAt")
    bytes: int
    analysis_status: str = Field(alias="analysisStatus")
    detection_count: int = Field(alias="detectionCount")
    top_confidence: Optional[float] = Field(default=None, alias="topConfidence")
    top_species: Optional[str] = Field(default=None, alias="topSpecies")
    t_start_us: Optional[int] = Field(default=None, alias="tStartUs")
    t_end_us: Optional[int] = Field(default=None, alias="tEndUs")

    model_config = ConfigDict(populate_by_name=True)


class NodeAudioSummary(BaseModel):
    """Per-node aggregate of audio_events — feeds the Analytics tab's stat
    cards. detection_rate is computed here (not in SQL) as
    pushes_with_detections / total_pushes."""
    node_id: Optional[str] = Field(default=None, alias="nodeId")
    total_pushes: int = Field(alias="totalPushes")
    triggered_pushes: int = Field(alias="triggeredPushes")
    pushes_with_detections: int = Field(alias="pushesWithDetections")
    pushes_zero_detections: int = Field(alias="pushesZeroDetections")
    detection_rate: float = Field(alias="detectionRate")
    last_push_at: Optional[str] = Field(default=None, alias="lastPushAt")
    last_trigger_at: Optional[str] = Field(default=None, alias="lastTriggerAt")
    avg_near_miss_confidence: Optional[float] = Field(default=None, alias="avgNearMissConfidence")

    model_config = ConfigDict(populate_by_name=True)


class AudioAnalytics(BaseModel):
    """Response for GET /api/analytics/audio."""
    summary: list[NodeAudioSummary]
    events: list[AudioEventRecord]

    model_config = ConfigDict(populate_by_name=True)


class SpeciesTdoaParams(BaseModel):
    """Tunable per-species TDOA orchestration parameters.

    Body for PUT /api/species-tdoa-params/{species_key} (create-or-update,
    consistent with this hub's other upsert-style PUT endpoints, e.g.
    NodePosition). The '__default__' species_key is a protected sentinel row
    used as the fallback when a detected species has no row of its own, or
    its row is disabled — see db.get_effective_species_tdoa_params(). It
    cannot be deleted or disabled (enforced in routes.py, not here).
    """
    enabled: bool = Field(default=True)
    correlation_method: str = Field(
        default="gcc_phat",
        alias="correlationMethod",
        description="'gcc_phat' (broadband, sharp-onset calls) or "
                     "'onset_envelope' (periodic/narrowband calls at risk of "
                     "cycle-slip/phase ambiguity with plain cross-correlation, "
                     "e.g. Pheasant Coucal). Plain string rather than an enum "
                     "so new methods can be added without a redeploy once the "
                     "orchestration code implements them.",
    )
    onset_detection_method: str = Field(
        default="global_peak",
        alias="onsetDetectionMethod",
        description="Matches clap_sync_check.py's detect_onset (picks the "
                     "global energy peak, not the first sample above "
                     "threshold). Only one implementation exists today; kept "
                     "as a free string, not an enum, for the same "
                     "no-redeploy-to-extend reason as correlation_method.",
    )
    onset_threshold_factor: float = Field(
        default=8.0, gt=2.0, alias="onsetThresholdFactor",
        description="Multiple of background RMS the onset envelope's "
                     "global peak must exceed to count as a real transient. "
                     "8.0 was tuned for hand-clap field validation "
                     "(tools/clap_sync_check.py), not bird calls — see "
                     "docs/tdoa-correlation-design-notes.md. Lowering this "
                     "catches quieter/filtered calls but increases the risk "
                     "of locking onto a louder non-target transient (wind, "
                     "insects, echo) instead of the real one — the "
                     "'global_peak' detector has no defense against that. "
                     "gt=2.0 is a floor against an operator zeroing this out "
                     "by accident, not a claim that low-single-digit values "
                     "are safe.",
    )
    freq_band_low_hz: Optional[float] = Field(
        default=None, alias="freqBandLowHz",
        description="Bandpass low edge (Hz) applied before onset detection. "
                     "Null (with freq_band_high_hz) means no filtering — "
                     "today's default, unfiltered broadband behavior. Both "
                     "must be set together to take effect (see "
                     "onset_detection.py).",
    )
    freq_band_high_hz: Optional[float] = Field(
        default=None, alias="freqBandHighHz",
        description="Bandpass high edge (Hz) — see freq_band_low_hz.",
    )
    pull_window_s: float = Field(
        default=3.0, alias="pullWindowS",
        description="Duration requested from neighbour nodes for this "
                     "species. The orchestration layer floors this (and the "
                     "margins below) to cover the array's max inter-node "
                     "sound travel time regardless of this value — at 150m "
                     "baseline, ~0.44s — so the real arrival at a far node "
                     "is never clipped by an under-tuned species row.",
    )
    window_margin_pre_ms: float = Field(default=500.0, alias="windowMarginPreMs")
    window_margin_post_ms: float = Field(default=500.0, alias="windowMarginPostMs")
    min_corroborating_nodes: int = Field(
        default=4, ge=4, alias="minCorroboratingNodes",
        description="Hard gate, not advisory — the closed-form solver itself "
                     "requires >=4 nodes (tdoa_solver.solve), so this can "
                     "only raise the bar above the solver's own floor, never "
                     "lower it.",
    )
    notes: Optional[str] = Field(default=None)

    model_config = ConfigDict(populate_by_name=True)


class SpeciesTdoaParamsRecord(SpeciesTdoaParams):
    """SpeciesTdoaParams plus the key and audit fields — returned by
    GET/PUT, never accepted as a request body."""
    species_key: str = Field(alias="speciesKey")
    updated_at: str = Field(alias="updatedAt")

    model_config = ConfigDict(populate_by_name=True)


class TriggerEventRecord(BaseModel):
    """One AudioTrigger dual-gate block — returned by GET /api/analytics/trigger-diag.

    Pulled from a node's GET /app/api/trigger-diag ring buffer (see
    TriggerDiagnostics.hpp on the node side). Only "interesting" blocks are
    ever recorded by the node — either gate ratio >= 1.5, or the trigger
    fired — so this is near-miss + fire data, not a continuous log.
    """
    id: int
    node_id: Optional[str] = Field(default=None, alias="nodeId")
    t_us: int = Field(alias="tUs")
    energy_ratio: float = Field(alias="energyRatio")
    flux_ratio: float = Field(alias="fluxRatio")
    fired: bool

    model_config = ConfigDict(populate_by_name=True)


class NodeTriggerSummary(BaseModel):
    """Per-node aggregate of trigger_events — near-miss vs. fire counts."""
    node_id: Optional[str] = Field(default=None, alias="nodeId")
    total_rows: int = Field(alias="totalRows")
    fired_rows: int = Field(alias="firedRows")
    near_miss_rows: int = Field(alias="nearMissRows")
    avg_energy_ratio: Optional[float] = Field(default=None, alias="avgEnergyRatio")
    avg_flux_ratio: Optional[float] = Field(default=None, alias="avgFluxRatio")
    last_t_us: Optional[int] = Field(default=None, alias="lastTUs")

    model_config = ConfigDict(populate_by_name=True)


class TriggerDiagAnalytics(BaseModel):
    """Response for GET /api/analytics/trigger-diag."""
    summary: list[NodeTriggerSummary]
    events: list[TriggerEventRecord]

    model_config = ConfigDict(populate_by_name=True)


class RatioHistogramBucket(BaseModel):
    """One bin of a trigger_ratio_histogram() result.

    bucket_start is the bin's lower edge in ratio units. The last bucket for
    a given histogram is an open-ended "≥ max_ratio" overflow, not a true
    fixed-width bin — see trigger_ratio_histogram()'s docstring.
    """
    bucket_start: float = Field(alias="bucketStart")
    count: int
    fired_count: int = Field(alias="firedCount")

    model_config = ConfigDict(populate_by_name=True)


class RatioHistogram(BaseModel):
    bucket_width: float = Field(alias="bucketWidth")
    max_ratio: float = Field(alias="maxRatio")
    buckets: list[RatioHistogramBucket]

    model_config = ConfigDict(populate_by_name=True)


class TriggerHistogramResponse(BaseModel):
    """Response for GET /api/analytics/trigger-diag/histogram.

    Distribution of near-miss/fire ratios over an explicit time range —
    scoped to raw trigger_events, so since_us can't reach further back than
    TRIGGER_EVENTS_RETENTION_HOURS. See trigger_ratio_histogram() in db.py.
    """
    node_id: Optional[str] = Field(default=None, alias="nodeId")
    since_us: int = Field(alias="sinceUs")
    until_us: int = Field(alias="untilUs")
    energy: RatioHistogram
    flux: RatioHistogram

    model_config = ConfigDict(populate_by_name=True)


class TriggerRollupBucket(BaseModel):
    """One row of trigger_event_rollups — a node's 1-minute activity summary."""
    node_id: Optional[str] = Field(default=None, alias="nodeId")
    bucket_start_us: int = Field(alias="bucketStartUs")
    entry_count: int = Field(alias="entryCount")
    fired_count: int = Field(alias="firedCount")
    energy_ratio_min: float = Field(alias="energyRatioMin")
    energy_ratio_avg: float = Field(alias="energyRatioAvg")
    energy_ratio_max: float = Field(alias="energyRatioMax")
    flux_ratio_min: float = Field(alias="fluxRatioMin")
    flux_ratio_avg: float = Field(alias="fluxRatioAvg")
    flux_ratio_max: float = Field(alias="fluxRatioMax")

    model_config = ConfigDict(populate_by_name=True)


class TriggerRollupResponse(BaseModel):
    """Response for GET /api/analytics/trigger-diag/rollups.

    Per-minute activity over time, from trigger_event_rollups — unlike the
    histogram, this survives indefinitely (rollups aren't pruned), so it can
    answer "when did this happen" over days/weeks, not just the raw
    retention window. See list_trigger_rollups() in db.py.
    """
    buckets: list[TriggerRollupBucket]

    model_config = ConfigDict(populate_by_name=True)


class LatLon(BaseModel):
    lat: float
    lon: float


class PositionRelative(BaseModel):
    eM: float
    nM: float
    altM: float


class GpsFix(BaseModel):
    lat: float
    lon: float
    altM: Optional[float] = None


class GpsView(BaseModel):
    locked: bool
    satellites: Optional[int] = None
    emaN: Optional[int] = Field(
        default=None, description="Samples admitted into the hub-side GPS EMA"
    )
    divergenceM: Optional[float] = None
    divergenceN: Optional[float] = None
    divergenceE: Optional[float] = None
    divergenceAlt: Optional[float] = None
    # Two views of the node's GPS-derived absolute position — see
    # status_mapper._fix. `live` is the raw current fix reported by the
    # node; `ema` is the hub-side smoothed estimate that replaced the old
    # node-side centroid/EMA split (see
    # GPS-TELEMETRY-SIMPLIFICATION-PROPOSAL.md in sound-capture-node).
    live: Optional[GpsFix] = None
    ema: Optional[GpsFix] = None


class ClockView(BaseModel):
    source: str
    accuracyUs: Optional[float] = None
    syncAgeMs: Optional[float] = None
    offsetUs: Optional[float] = None
    kalmanSettled: Optional[bool] = None
    valid: Optional[bool] = None


class AudioView(BaseModel):
    bufferCapacityS: Optional[float] = None
    bufferUsedS: Optional[float] = None
    sampleRateHz: Optional[int] = None
    bitDepth: Optional[int] = None
    lastTriggerAt: Optional[str] = None
    running: Optional[bool] = None


class EspNowView(BaseModel):
    rssi: Optional[int] = None
    hopCount: Optional[int] = None
    lastHeartbeatAt: Optional[str] = None


class NodeView(BaseModel):
    """Merged registry record + live polled status + hub position — what the
    frontend consumes.

    Structured fields (status/latLon/positionRelative/gps/clock/audio/
    espNow/flags) are derived from `raw_status` + hub position by
    `status_mapper.map_status`. `raw_status` is retained as a passthrough
    for debugging.
    """
    model_config = ConfigDict(populate_by_name=True)

    id: str
    hostname: str
    ip_address: Optional[str] = Field(default=None, alias="ipAddress")
    role: str = "UNKNOWN"
    discovery_method: Literal["mdns", "manual", "self_registered"] = Field(default="mdns", alias="discoveryMethod")
    approval_status: Literal["pending", "approved", "rejected"] = Field(
        default="pending", alias="approvalStatus"
    )
    configured: bool = False
    reachable: bool = False
    last_seen_at: Optional[str] = Field(default=None, alias="lastSeenAt")
    raw_status: Optional[dict] = Field(default=None, alias="rawStatus")

    # --- Derived/structured view (see status_mapper.map_status) ---
    status: Literal["online", "degraded", "offline"] = "offline"
    lat_lon: Optional[LatLon] = Field(default=None, alias="latLon")
    position_relative: Optional[PositionRelative] = Field(default=None, alias="positionRelative")
    position_known: bool = Field(default=False, alias="positionKnown")
    position_status: Literal["surveyed", "estimated"] = Field(
          default="estimated", alias="positionStatus"
    )
    is_origin: bool = Field(default=False, alias="isOrigin")
    # Hub-computed: horizontal distance (metres) between stored survey origin
    # and the node's GPS centroid. Non-null only when is_origin=True and GPS
    # centroid is available. One input to position trust — not a composite.
    survey_disagreement_m: Optional[float] = Field(
        default=None, alias="surveyDisagreementM"
    )
    gps: Optional[GpsView] = None
    clock: Optional[ClockView] = None
    audio: Optional[AudioView] = None
    esp_now: Optional[EspNowView] = Field(default=None, alias="espNow")
    flags: list[str] = []
    firmware_version: Optional[str] = Field(default=None, alias="firmwareVersion")

    # --- Heap telemetry from the node's self-registration POST (sent over
    # plain HTTP, independent of the HTTPS status endpoint — stays available
    # even when /app/api/status is failing). See HubRegistrar.cpp.
    reg_heap_free_bytes: Optional[int] = Field(default=None, alias="regHeapFreeBytes")
    reg_heap_min_free_bytes: Optional[int] = Field(default=None, alias="regHeapMinFreeBytes")
    reg_heap_at: Optional[str] = Field(default=None, alias="regHeapAt")

    # --- HTTPS socket-pool telemetry from the same self-registration POST.
    # See EspHttpServer::activeSocketCount() / HubRegistrar.cpp.
    reg_https_active_sockets: Optional[int] = Field(default=None, alias="regHttpsActiveSockets")
    reg_https_max_sockets: Optional[int] = Field(default=None, alias="regHttpsMaxSockets")
