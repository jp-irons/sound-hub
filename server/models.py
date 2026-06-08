"""Pydantic schemas for the API."""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ManualNodeRequest(BaseModel):
    """Body for POST /api/nodes/manual — fallback discovery path."""
    host: str  # hostname or bare IP; we hit http://<host>/app/api/status to validate


class NodeConfigRequest(BaseModel):
    """Body for POST /api/nodes/{id}/configure — proxied verbatim (minus
    unset fields) to the node's own POST /app/api/node-config.

    All fields optional: the operator edits a subset in the modal and we
    forward only what they changed, mirroring the node-side handler which
    treats each field as independently settable. See NodeConfigHandler.cpp.
    """
    role: Optional[Literal["primary", "node", "remote"]] = None
    posE: Optional[float] = None
    posN: Optional[float] = None
    posAlt: Optional[float] = None
    position_status: Optional[Literal["surveyed", "estimated"]] = Field(default=None, alias="positionStatus")
    is_origin: Optional[bool] = Field(default=None, alias="isOrigin")
    origin_lat: Optional[float] = Field(default=None, alias="originLat")
    origin_lon: Optional[float] = Field(default=None, alias="originLon")
    origin_alt: Optional[float] = Field(default=None, alias="originAlt")

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
    # Surveyed/configured absolute position (primary node only — `null`
    # elsewhere). Authoritative when present; preferred over the estimates
    # above for `latLon`/the map marker. See status_mapper._origin.
    origin: Optional[GpsFix] = None


class ClockView(BaseModel):
    source: str
    accuracyUs: Optional[float] = None
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
    """Merged registry record + live polled status — what the frontend consumes.

    The structured fields below (status/latLon/positionRelative/gps/clock/
    audio/espNow/flags) are derived from `raw_status` by `status_mapper`,
    translating the firmware's real `/app/api/status` schema onto the shape
    the frontend's mock data already established. `raw_status` is retained
    as a passthrough for debugging — the frontend should prefer the
    structured fields, which remain stable even if the firmware schema shifts.
    """
    model_config = ConfigDict(populate_by_name=True)

    id: str
    hostname: str
    ip_address: Optional[str] = Field(default=None, alias="ipAddress")
    role: str = "UNKNOWN"
    discovery_method: Literal["mdns", "manual"] = Field(default="mdns", alias="discoveryMethod")
    # Operator admission gate — being seen on the network (mDNS/manual) only
    # gets a node to "pending"; an explicit approve is required before it's
    # treated as part of the active array (polling, map, TDOA). Rejected
    # nodes are retained (not deleted) so the decision is reversible.
    approval_status: Literal["pending", "approved", "rejected"] = Field(
        default="pending", alias="approvalStatus"
    )
    configured: bool = False
    reachable: bool = False
    last_seen_at: Optional[str] = Field(default=None, alias="lastSeenAt")
    raw_status: Optional[dict] = Field(default=None, alias="rawStatus")

    # --- Derived/structured view (see status_mapper.map_status), aliased to
    # camelCase so the frontend can consume this directly — same naming
    # convention mockNodes.js already established. ---
    status: Literal["online", "degraded", "offline"] = "offline"
    lat_lon: Optional[LatLon] = Field(default=None, alias="latLon")
    position_relative: Optional[PositionRelative] = Field(default=None, alias="positionRelative")
    position_known: bool = Field(default=False, alias="positionKnown")
    # Operator-set provenance for the node's position: "surveyed" (confirmed
    # ground truth, fixed anchor) vs "estimated" (provisional, refined by
    # ongoing calibration). Distinct from `position_known` — a position can
    # be known (relative offset configured) but still only an estimate.
    position_status: Literal["surveyed", "estimated"] = Field(default="estimated", alias="positionStatus")
    gps: Optional[GpsView] = None
    clock: Optional[ClockView] = None
    audio: Optional[AudioView] = None
    esp_now: Optional[EspNowView] = Field(default=None, alias="espNow")
    flags: list[str] = []

    # Not yet sourced anywhere (firmware doesn't report it) — present so the
    # frontend's existing display code has a stable field to read "—" from.
    firmware_version: Optional[str] = Field(default=None, alias="firmwareVersion")
