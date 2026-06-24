"""Pydantic schemas for the API."""
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


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


class AudioSampleRequest(BaseModel):
    """Body for POST /api/nodes/{id}/sample — trigger an audio pull from a node."""
    t_start_us: int = Field(alias="tStartUs", description="UTC µs — inclusive segment start")
    t_end_us:   int = Field(alias="tEndUs",   description="UTC µs — exclusive segment end")

    model_config = ConfigDict(populate_by_name=True)


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
    # Operator-surveyed absolute coordinates — alternative to GPS centroid
    # when setting array origin.  All three must be provided together.
    surveyed_lat: Optional[float] = Field(default=None, alias="surveyedLat")
    surveyed_lon: Optional[float] = Field(default=None, alias="surveyedLon")
    surveyed_alt: Optional[float] = Field(default=None, alias="surveyedAlt")

    model_config = ConfigDict(populate_by_name=True)


class ArrayOrigin(BaseModel):
    """Hub-level geographic datum for the node array (array_origin table).

    Set by POST /api/origin/set-from-node/{node_id} (back-projected from a
    node's GPS centroid + surveyed array offset) or by PUT /api/origin
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
    centroidN: Optional[int] = None
    centroidStddevM: Optional[float] = None
    divergenceM: Optional[float] = None
    divergenceN: Optional[float] = None
    divergenceE: Optional[float] = None
    divergenceAlt: Optional[float] = None
    # Three views of the node's GPS-derived absolute position — see
    # status_mapper._fix. `centroid` is the most stable estimate.
    live: Optional[GpsFix] = None
    ema: Optional[GpsFix] = None
    centroid: Optional[GpsFix] = None


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
