"""Maps the firmware's real `/app/api/status` schema onto the structured
shape the frontend expects (see src/data/mockNodes.js for the reference shape).

Why this exists: the mock UI was built against a guessed schema before any
real hardware was reachable. Now that we've seen a live response from
soundcapture-ed5de4, several things differ from the guess:

  - `audio.sampleRateHz` is 48000, not 16000 — and that's *correct*, not an
    oversight: at 16kHz, Nyquist (8kHz) would be below the 16-18kHz
    calibration-chirp band, so the chirps couldn't be captured at all. 48kHz
    gives headroom to ~24kHz and matches BirdNET's native processing rate.
  - GPS reporting is much richer than guessed — three views (`gps` live,
    `gpsEma`, `gpsCentroid`) rather than one flat object.
  - The firmware does not (yet) report a numeric clock accuracy, firmware
    version, ESP-NOW link stats, or trigger timestamps. Where the mock UI
    expects a number we don't have, we pass `None` through — see the
    NodeCard/NodeDetail tweaks for graceful "—" rendering.

This module is the single place that translation happens, so as the firmware
schema evolves we touch one file rather than hunting through routes/components.
"""
from typing import Optional


def _node_info(raw: dict) -> dict:
    return raw.get("node") or {}


def _role(raw_role: Optional[str]) -> str:
    """Normalise the firmware's role vocabulary onto the UI's.

    Firmware reports `Role::Primary` / `Role::Node` (serialised e.g. as
    "primary" / "node"). The frontend (NodeSidebar grouping, badge CSS —
    see badge-primary/badge-leaf in index.css) was built around "PRIMARY"
    / "LEAF". Without this mapping a secondary node's role normalises to
    "NODE", which matches neither bucket — it's counted in the online
    total but rendered in neither sidebar section. Anything that isn't
    primary is a leaf in the UI's two-role architecture.
    """
    if (raw_role or "").strip().upper() == "PRIMARY":
        return "PRIMARY"
    if not raw_role:
        return "UNKNOWN"
    return "LEAF"


def _position(raw: dict) -> tuple[Optional[dict], bool]:
    info = _node_info(raw)
    e, n, alt = info.get("posE"), info.get("posN"), info.get("posAlt")
    known = e is not None and n is not None and alt is not None
    return ({"eM": e, "nM": n, "altM": alt} if known else None), known


def _origin(raw: dict) -> Optional[dict]:
    """Pull the configured/surveyed absolute position out of `node`.

    Firmware now reports `node.originLat/originLon/originAlt` — populated
    only on the primary node (its surveyed position, set at configuration
    time), `null` everywhere else. This is the authoritative absolute
    position when present; everything else (centroid/EMA/live fix) is just
    an estimate of where the GPS *thinks* the node is.
    """
    info = _node_info(raw)
    lat, lon = info.get("originLat"), info.get("originLon")
    if lat is None or lon is None:
        return None
    return {"lat": lat, "lon": lon, "altM": info.get("originAlt")}


def _lat_lon(raw: dict) -> Optional[dict]:
    """Prefer the surveyed origin (authoritative, primary-only), then the
    long-running centroid average (far more stable than an instantaneous
    fix for a node that's been sitting in one spot for hours), then the
    live fix as a last resort."""
    origin = _origin(raw)
    if origin:
        return {"lat": origin["lat"], "lon": origin["lon"]}
    centroid = raw.get("gpsCentroid")
    if centroid and centroid.get("latitude") is not None:
        return {"lat": centroid["latitude"], "lon": centroid["longitude"]}
    gps = raw.get("gps")
    if gps and gps.get("available") and gps.get("latitude") is not None:
        return {"lat": gps["latitude"], "lon": gps["longitude"]}
    return None


def _fix(obj: Optional[dict]) -> Optional[dict]:
    """Pull {lat, lon, altM} out of a raw gps/gpsEma/gpsCentroid sub-object."""
    if not obj or obj.get("latitude") is None:
        return None
    return {"lat": obj["latitude"], "lon": obj["longitude"], "altM": obj.get("altitudeM")}


def _gps(raw: dict) -> Optional[dict]:
    gps = raw.get("gps")
    if not gps or not gps.get("available"):
        return None
    ema = raw.get("gpsEma") or {}
    centroid = raw.get("gpsCentroid") or {}
    return {
        "locked": bool(gps.get("receiving")),
        "satellites": gps.get("satellites"),
        "centroidN": centroid.get("count"),
        "centroidStddevM": centroid.get("horizontalStddevM"),
        "divergenceM": ema.get("divergenceM"),
        "divergenceN": ema.get("divergenceN"),
        "divergenceE": ema.get("divergenceE"),
        "divergenceAlt": ema.get("divergenceAlt"),
        # Three views of "where the GPS *thinks* it is" — the live
        # instantaneous fix (jittery), the EMA-smoothed fix, and the
        # long-running centroid average (most stable estimate). `origin`
        # is different in kind: it's the surveyed/configured absolute
        # position (primary node only) — authoritative, not an estimate —
        # and is what `latLon`/the map marker prefers when present.
        "live": _fix(gps),
        "ema": _fix(ema),
        "centroid": _fix(centroid),
        "origin": _origin(raw),
    }


def _clock(raw: dict) -> dict:
    clock = raw.get("clock") or {}
    gps = raw.get("gps") or {}

    if clock.get("ppsDisciplined"):
        source = "GPS_PPS"
    elif gps.get("available"):
        source = "GPS_NMEA"
    else:
        source = "ESPNOW_KALMAN"

    return {
        "source": source,
        # Not yet reported by firmware — PPS discipline isn't wired up on
        # this build (see firmware status memory). Surfacing `None` rather
        # than a guessed number; UI shows "—".
        "accuracyUs": None,
        "offsetUs": None,
        "kalmanSettled": None,
        # Extra field beyond the mock shape — handy to show directly.
        "valid": clock.get("valid"),
    }


def _audio(raw: dict) -> Optional[dict]:
    audio = raw.get("audio")
    if not audio:
        return None
    return {
        "bufferCapacityS": audio.get("bufferCapacitySecs"),
        "bufferUsedS": audio.get("bufferFillSecs"),
        "sampleRateHz": audio.get("sampleRateHz"),
        # Not reported — ICS-43434 is a 24-bit MEMS mic; firmware likely
        # delivers 16- or 32-bit samples over I2S. Leave unknown rather
        # than guess; UI shows "—".
        "bitDepth": None,
        "lastTriggerAt": None,
        "running": audio.get("running"),
    }


def _position_status(raw: dict) -> str:
    """Operator-set provenance flag for the node's E/N/Alt position.

    Firmware reports `node.positionStatus` as "surveyed" (operator-confirmed
    ground truth — treated as a fixed anchor) or "estimated" (provisional,
    refined by ongoing calibration). Older firmware that predates this field
    won't report it at all — default to "estimated" rather than implying a
    confidence the node never claimed.
    """
    info = _node_info(raw)
    value = (info.get("positionStatus") or "").strip().lower()
    return "surveyed" if value == "surveyed" else "estimated"


def _flags(position_known: bool, clock: dict, reachable: bool) -> list[str]:
    flags = []
    if not reachable:
        # A momentary poll miss shouldn't blank the UI — the registry retains
        # the last raw_status (see registry.update_live_status), and we keep
        # mapping it below. We just flag that it may be stale.
        flags.append("UNREACHABLE")
    if not position_known:
        flags.append("POSITION_UNKNOWN")
    if clock.get("valid") is False:
        flags.append("CLOCK_INVALID")
    return flags


def _status(reachable: bool, flags: list[str]) -> str:
    if not reachable:
        return "offline"
    if flags:
        return "degraded"
    return "online"


# Brisbane-latitude (~ -27.5°) flat-earth scale factors — the same ones the
# firmware uses for its EMA divergence (NEU) calc. Fine for projecting
# offsets at the <1km scale of this property; would need a proper geodesic
# if the array ever spans enough latitude for the approximation to drift.
_M_PER_DEG_LAT = 111_320.0
_M_PER_DEG_LON = 98_740.0


def _offset_to_latlon(origin: dict, e_m: float, n_m: float) -> dict:
    """Project a local-tangent-plane E/N offset (metres) onto an absolute
    lat/lon, treating `origin` as the tangent point."""
    return {
        "lat": origin["lat"] + (n_m / _M_PER_DEG_LAT),
        "lon": origin["lon"] + (e_m / _M_PER_DEG_LON),
    }


def derive_relative_positions(mapped: list[dict]) -> None:
    """Fill in `lat_lon` for nodes that are positioned only via a relative
    E/N/Alt offset from the primary, by projecting that offset onto the
    primary's absolute position. Mutates the mapped dicts in place.

    Why: only the primary reports GPS — leaf nodes have no `lat_lon` of
    their own, so `MapView` (which only plots nodes with `lat_lon`) can't
    place them even though their position is known relative to the primary.
    This derives a plottable absolute position for them.

    This is explicitly a *derived* position, not a surveyed one — we tag it
    with the `POSITION_DERIVED` flag so the UI can (later) distinguish
    "surveyed/GPS-determined" from "calculated from relative offset" rather
    than implying the same precision/provenance for both.
    """
    origin = None
    for m in mapped:
        if m.get("role") == "PRIMARY":
            gps = m.get("gps") or {}
            origin = gps.get("origin") or gps.get("centroid") or m.get("lat_lon")
            break
    if not origin:
        return

    for m in mapped:
        if m.get("role") == "PRIMARY" or m.get("lat_lon") or not m.get("position_relative"):
            continue
        rel = m["position_relative"]
        m["lat_lon"] = _offset_to_latlon(origin, rel["eM"], rel["nM"])
        m["flags"] = [*m.get("flags", []), "POSITION_DERIVED"]


def map_status(role: str, reachable: bool, raw_status: Optional[dict]) -> dict:
    """Build the set of derived/display fields the frontend expects.

    Returns a dict suitable for spreading into NodeView — callers merge this
    with the persisted identity fields (id, hostname, ip_address, ...).

    IMPORTANT: `reachable` reflects only the *most recent* poll. A single
    missed cycle (WiFi blip, timeout) sets it False, but `registry` retains
    the last-known `raw_status` rather than clearing it. We deliberately keep
    mapping that cached status here — flagging it UNREACHABLE/degraded rather
    than nulling out gps/clock/audio — because:
      (a) last-known values (buffer fill, GPS lock, etc.) are operationally
          more useful than a blank panel during a transient drop, and
      (b) several frontend components (e.g. TopBar's "last trigger" scan)
          assume `audio`/`gps`/`clock` are present whenever a node has ever
          reported — abruptly nulling them on every routine poll miss was
          producing intermittent null-deref crashes (blank/black screen,
          recoverable only by reload).
    Only a node that has *never* successfully reported gets fully-null fields.
    """
    if not raw_status:
        return {
            "status": "offline",
            "role": role,
            "lat_lon": None,
            "position_relative": None,
            "position_known": False,
            "position_status": "estimated",
            "gps": None,
            "clock": None,
            "audio": None,
            "esp_now": None,
            "flags": ["UNREACHABLE"],
        }

    position_relative, position_known = _position(raw_status)
    clock = _clock(raw_status)
    flags = _flags(position_known, clock, reachable)
    info = _node_info(raw_status)

    return {
        "status": _status(reachable, flags),
        # Prefer the role the node itself reports — more authoritative than
        # whatever the registry guessed at discovery time.
        "role": _role(info.get("role") or role),
        "lat_lon": _lat_lon(raw_status),
        "position_relative": position_relative,
        "position_known": position_known,
        "position_status": _position_status(raw_status),
        "gps": _gps(raw_status),
        "clock": clock,
        "audio": _audio(raw_status),
        # Not present in the primary node's status — TODO confirm whether
        # leaf nodes report ESP-NOW link stats once one is on the network.
        "esp_now": None,
        "flags": flags,
    }
