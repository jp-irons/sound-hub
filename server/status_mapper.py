"""Maps the firmware's real `/app/api/status` schema onto the structured
shape the frontend expects (see src/data/mockNodes.js for the reference shape).

Architecture note (2026-06-09): nodes no longer report position data
(`posE/N/Alt`, `positionStatus`, `isOrigin`, `originLat/Lon/Alt`,
`surveyDisagreementM`). All position ownership has moved to the hub's
`node_positions` SQLite table. Nodes report only GPS telemetry (raw fix,
EMA, centroid) which the hub passthrouh for display and uses to compute
`surveyDisagreementM` against the stored position.

Role vocabulary has also changed: firmware now reports `node.isBroker` (bool)
rather than `node.role` (enum). The frontend still uses "PRIMARY"/"LEAF" for
grouping/badge CSS; this module maps isBroker onto that vocabulary so Track C
(frontend reshape) can happen independently.
"""
from typing import Optional


def _node_info(raw: dict) -> dict:
    return raw.get("node") or {}


def _role(raw: dict) -> str:
    """Map firmware's isBroker bool onto the frontend's PRIMARY/LEAF vocabulary.

    Firmware (post Track A) reports `node.isBroker: bool`. The frontend's
    NodeSidebar grouping and badge CSS use "PRIMARY"/"LEAF" — preserved here
    so Track C can reshape the frontend independently. Absent isBroker
    (e.g. a node still running pre-refactor firmware) returns "UNKNOWN".
    """
    info = _node_info(raw)
    is_broker = info.get("isBroker")
    if is_broker is True:
        return "BROKER"
    if is_broker is False:
        return "LEAF"
    return "UNKNOWN"


def _lat_lon_from_gps(raw: dict) -> Optional[dict]:
    """Derive a GPS-based lat/lon for display when no surveyed position exists.

    Used as a fallback before the array origin is established — e.g. during
    initial deployment when nodes are online but not yet surveyed.  Once the
    hub array_origin is set, derive_relative_positions() projects all
    surveyed nodes from their stored E/N offsets and overrides this value.

    Priority: GPS centroid (most stable) → live fix.
    """
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
        # long-running centroid average (most stable estimate).
        "live": _fix(gps),
        "ema": _fix(ema),
        "centroid": _fix(centroid),
    }


_FIRMWARE_CLOCK_SOURCE = {
    "GPS PPS":                "GPS_PPS",
    "GPS NMEA":               "GPS_NMEA",
    "Network - GPS PPS":      "NETWORK_GPS_PPS",
    "Network - GPS NMEA":     "NETWORK_GPS_NMEA",
    "Network - free running": "NETWORK_FREE_RUNNING",
    "Free running":           "FREE_RUNNING",
}


def _clock(raw: dict) -> dict:
    clock = raw.get("clock") or {}
    gps   = raw.get("gps")   or {}

    fw_source = clock.get("source")
    if fw_source and fw_source in _FIRMWARE_CLOCK_SOURCE:
        source = _FIRMWARE_CLOCK_SOURCE[fw_source]
    elif clock.get("ppsDisciplined"):
        source = "GPS_PPS"
    elif gps.get("available"):
        source = "GPS_NMEA"
    else:
        source = "FREE_RUNNING"

    return {
        "source":        source,
        "accuracyUs":    clock.get("uncertaintyUs"),
        "syncAgeMs":     clock.get("syncAgeMs"),
        "offsetUs":      None,
        "kalmanSettled": None,
        "valid":         clock.get("valid"),
    }


def _audio(raw: dict) -> Optional[dict]:
    audio = raw.get("audio")
    if not audio:
        return None
    return {
        "bufferCapacityS": audio.get("bufferCapacitySecs"),
        "bufferUsedS": audio.get("storedSecs"),
        "sampleRateHz": audio.get("sampleRateHz"),
        "bitDepth": audio.get("bitsPerSample"),
        "lastTriggerAt": None,
        "running": audio.get("running"),
    }


def _position_from_hub(node_pos: Optional[dict]) -> tuple[Optional[dict], bool]:
    """Extract relative position (E/N/Alt) from the hub's position record."""
    if not node_pos:
        return None, False
    e, n, alt = node_pos.get("pos_e"), node_pos.get("pos_n"), node_pos.get("pos_alt")
    known = e is not None and n is not None and alt is not None
    return ({"eM": e, "nM": n, "altM": alt} if known else None), known


def _flags(position_known: bool, clock: dict, reachable: bool) -> list[str]:
    flags = []
    if not reachable:
        flags.append("UNREACHABLE")
    if not position_known:
        flags.append("POSITION_UNKNOWN")
    if clock.get("valid") is False:
        if clock.get("source") == "NETWORK_FREE_RUNNING":
            flags.append("CLOCK_NO_UTC")
        else:
            flags.append("CLOCK_INVALID")
    return flags


def _status(reachable: bool, flags: list[str]) -> str:
    if not reachable:
        return "offline"
    if flags:
        return "degraded"
    return "online"


# Brisbane-latitude (~-27.5°) flat-earth scale factors — used for EMA
# divergence and survey disagreement computation. Fine at <1 km scale.
_M_PER_DEG_LAT = 111_320.0
_M_PER_DEG_LON = 98_740.0


def _offset_to_latlon(origin: dict, e_m: float, n_m: float) -> dict:
    """Project a local-tangent-plane E/N offset (metres) onto an absolute
    lat/lon, treating `origin` as the tangent point."""
    return {
        "lat": origin["lat"] + (n_m / _M_PER_DEG_LAT),
        "lon": origin["lon"] + (e_m / _M_PER_DEG_LON),
    }


def survey_disagreement_m(
    node_pos: Optional[dict],
    raw: dict,
    array_origin: Optional[dict] = None,
) -> Optional[float]:
    """Compute the horizontal distance between the node's projected survey
    position and its GPS centroid estimate, in metres.

    Meaningful for any node that has both a surveyed E/N position and an
    active GPS centroid — not limited to the is_origin node.  Returns None
    if array_origin is not set, the node has no position, or GPS centroid
    is unavailable.

    This is one input to operator confidence in the surveyed position — not
    a single composite trust verdict.
    """
    if not node_pos or array_origin is None:
        return None
    pos_e = node_pos.get("pos_e")
    pos_n = node_pos.get("pos_n")
    if pos_e is None or pos_n is None:
        return None
    centroid = (raw or {}).get("gpsCentroid") or {}
    if centroid.get("latitude") is None:
        return None
    # Project the stored E/N position to lat/lon for comparison.
    surveyed = _offset_to_latlon(
        {"lat": array_origin["lat"], "lon": array_origin["lon"]}, pos_e, pos_n
    )
    dlat = centroid["latitude"] - surveyed["lat"]
    dlon = centroid["longitude"] - surveyed["lon"]
    return ((dlat * _M_PER_DEG_LAT) ** 2 + (dlon * _M_PER_DEG_LON) ** 2) ** 0.5


def derive_relative_positions(
    mapped: list[dict],
    array_origin: Optional[dict] = None,
) -> None:
    """Fill in `lat_lon` for all nodes that have a stored E/N/Alt position.
    Mutates the mapped dicts in place.

    `array_origin` is the hub-level geographic datum — a dict with keys
    `lat`, `lon`, `alt_m` from the `array_origin` DB table.  It is independent
    of any specific node; pass it in from routes._mapped_nodes().

    If `array_origin` is None (not yet configured), falls back to the GPS
    centroid of the first online BROKER node — a pre-survey display aid only,
    not used for TDOA.

    All nodes with a stored position_relative are projected from the datum
    the same way — there is no node that "is" the origin any more (the hub
    array origin is a standalone geographic datum, not tied to a node).
    """
    if array_origin is None:
        # Pre-survey fallback: use BROKER GPS centroid as an approximate datum.
        for m in mapped:
            if m.get("role") == "BROKER":
                gps = m.get("gps") or {}
                centroid = gps.get("centroid")
                if centroid:
                    array_origin = {"lat": centroid["lat"], "lon": centroid["lon"]}
                break

    if array_origin is None:
        return

    origin_latlon = {"lat": array_origin["lat"], "lon": array_origin["lon"]}

    for m in mapped:
        if not m.get("position_relative"):
            continue
        rel = m["position_relative"]
        m["lat_lon"] = _offset_to_latlon(origin_latlon, rel["eM"], rel["nM"])
        # Every node's lat/lon is projected from the hub array origin — flag
        # it so the UI shows that provenance rather than a GPS-centroid guess.
        m["flags"] = [*m.get("flags", []), "POSITION_DERIVED"]


def map_status(
    role: str,
    reachable: bool,
    raw_status: Optional[dict],
    node_pos: Optional[dict] = None,
    array_origin: Optional[dict] = None,
) -> dict:
    """Build the set of derived/display fields the frontend expects.

    `node_pos` is the hub's persisted position record for this node (from
    `db.get_node_position`), or None if no position has been set yet.

    `lat_lon` is left None here for nodes with a stored position — it will
    be filled by derive_relative_positions() once the hub array_origin is
    known.  For nodes without a stored position that happen to be online,
    _lat_lon_from_gps() provides a display fallback.

    Returns a dict suitable for spreading into NodeView — callers merge this
    with the persisted identity fields (id, hostname, ip_address, ...).
    """
    position_relative, position_known = _position_from_hub(node_pos)
    is_origin = bool(node_pos and node_pos.get("is_origin"))
    pos_status = (node_pos or {}).get("pos_status", "estimated")

    if not raw_status:
        # Node is offline — live telemetry unavailable, but hub-stored position
        # is still valid and must not be discarded.  derive_relative_positions()
        # will project lat_lon from the stored offset + hub array_origin.
        flags = ["UNREACHABLE"]
        if not position_known:
            flags.append("POSITION_UNKNOWN")
        return {
            "status": "offline",
            "role": role,
            "lat_lon": None,          # filled by derive_relative_positions()
            "position_relative": position_relative,
            "position_known": position_known,
            "position_status": pos_status,
            "is_origin": is_origin,
            "survey_disagreement_m": None,
            "gps": None,
            "clock": None,
            "audio": None,
            "esp_now": None,
            "firmware_version": None,
            "flags": flags,
        }

    clock = _clock(raw_status)
    audio = _audio(raw_status)
    flags = _flags(position_known, clock, reachable)
    if audio is not None and audio.get("running") is False:
        flags.append("AUDIO_STOPPED")

    # lat_lon: only set from GPS here for nodes without a stored position
    # (pre-survey display fallback).  Nodes with stored positions get their
    # lat_lon projected from the hub array_origin by derive_relative_positions().
    lat_lon = None if position_known else _lat_lon_from_gps(raw_status)

    return {
        "status": _status(reachable, flags),
        "role": _role(raw_status),
        "lat_lon": lat_lon,
        "position_relative": position_relative,
        "position_known": position_known,
        "position_status": pos_status,
        "is_origin": is_origin,
        "survey_disagreement_m": survey_disagreement_m(node_pos, raw_status, array_origin),
        "gps": _gps(raw_status),
        "clock": clock,
        "audio": audio,
        "esp_now": None,
        "firmware_version": _node_info(raw_status).get("firmwareVersion"),
        "flags": flags,
    }
