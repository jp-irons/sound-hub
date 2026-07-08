"""Maps the firmware's real `/app/api/status` schema onto the structured
shape the frontend expects (see src/data/mockNodes.js for the reference shape).

Architecture note (2026-06-09): nodes no longer report position data
(`posE/N/Alt`, `positionStatus`, `isOrigin`, `originLat/Lon/Alt`,
`surveyDisagreementM`). All position ownership has moved to the hub's
`node_positions` SQLite table.

Architecture note (2026-07-08 — GPS telemetry simplification, see
GPS-TELEMETRY-SIMPLIFICATION-PROPOSAL.md in sound-capture-node): nodes now
report only a raw current GPS fix (`gps`). The node-side long-term Welford
centroid and EMA (`gpsCentroid`/`gpsEma`) are gone — the hub maintains its
own in-memory GPS EMA per node instead (see `registry.get_gps_ema`), passed
into this module's functions as `gps_ema` rather than read out of
`raw_status`. `map_status`'s `survey_disagreement_m` output field and the
pre-survey array-origin fallback in `derive_relative_positions` both source
from that hub-side EMA now (see `_ema_survey_divergence`).

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


def _lat_lon_from_gps(raw: dict, gps_ema: Optional[dict]) -> Optional[dict]:
    """Derive a GPS-based lat/lon for display when no surveyed position exists.

    Used as a fallback before the array origin is established — e.g. during
    initial deployment when nodes are online but not yet surveyed.  Once the
    hub array_origin is set, derive_relative_positions() projects all
    surveyed nodes from their stored E/N offsets and overrides this value.

    Priority: hub-side GPS EMA (most stable) → live fix.
    """
    if gps_ema is not None:
        return {"lat": gps_ema["lat"], "lon": gps_ema["lon"]}
    gps = raw.get("gps")
    if gps and gps.get("available") and gps.get("latitude") is not None:
        return {"lat": gps["latitude"], "lon": gps["longitude"]}
    return None


def _fix(obj: Optional[dict]) -> Optional[dict]:
    """Pull {lat, lon, altM} out of a raw gps sub-object, or an already
    latitude/longitude/altitudeM-shaped dict (see _ema_fix)."""
    if not obj or obj.get("latitude") is None:
        return None
    return {"lat": obj["latitude"], "lon": obj["longitude"], "altM": obj.get("altitudeM")}


def _ema_fix(gps_ema: Optional[dict]) -> Optional[dict]:
    """Reshape a registry.get_gps_ema() result ({lat, lon, alt, n}) into the
    same {lat, lon, altM} shape _fix() produces from a raw gps object."""
    if gps_ema is None:
        return None
    return {"lat": gps_ema["lat"], "lon": gps_ema["lon"], "altM": gps_ema["alt"]}


def _gps(raw: dict, gps_ema: Optional[dict], divergence: Optional[dict]) -> Optional[dict]:
    gps = raw.get("gps")
    if not gps or not gps.get("available"):
        return None
    return {
        "locked": bool(gps.get("receiving")),
        "satellites": gps.get("satellites"),
        "emaN": gps_ema["n"] if gps_ema else None,
        "divergenceM": divergence["m"] if divergence else None,
        "divergenceN": divergence["n"] if divergence else None,
        "divergenceE": divergence["e"] if divergence else None,
        "divergenceAlt": divergence["alt"] if divergence else None,
        # Two views of "where the GPS *thinks* it is" — the live
        # instantaneous fix (jittery) and the hub-side EMA-smoothed estimate
        # (replaces the old node-side EMA/centroid split — see
        # GPS-TELEMETRY-SIMPLIFICATION-PROPOSAL.md).
        "live": _fix(gps),
        "ema": _ema_fix(gps_ema),
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


def _ema_survey_divergence(
    node_pos: Optional[dict],
    array_origin: Optional[dict],
    gps_ema: Optional[dict],
) -> Optional[dict]:
    """Delta between a node's persisted position (projected through the array
    origin) and its current hub-side GPS EMA, decomposed N/E/Alt plus the
    horizontal magnitude — {"m", "n", "e", "alt"} in metres.

    This is the live equivalent of what survey_disagreement_m() used to
    compute against the firmware's node-side GPS centroid — same idea, fed
    the hub-side EMA instead (see GPS-TELEMETRY-SIMPLIFICATION-PROPOSAL.md).
    Meaningful for any node that has both a surveyed E/N position and a
    converged GPS EMA — not limited to any particular node. Returns None if
    array_origin isn't set, the node has no stored E/N position, or the EMA
    hasn't converged yet.
    """
    if not node_pos or array_origin is None or gps_ema is None:
        return None
    pos_e = node_pos.get("pos_e")
    pos_n = node_pos.get("pos_n")
    pos_alt = node_pos.get("pos_alt")
    if pos_e is None or pos_n is None:
        return None
    # Project the stored E/N position to lat/lon for comparison.
    surveyed = _offset_to_latlon(
        {"lat": array_origin["lat"], "lon": array_origin["lon"]}, pos_e, pos_n
    )
    d_n = (gps_ema["lat"] - surveyed["lat"]) * _M_PER_DEG_LAT
    d_e = (gps_ema["lon"] - surveyed["lon"]) * _M_PER_DEG_LON
    d_alt = (gps_ema["alt"] - (array_origin["alt_m"] + pos_alt)) if pos_alt is not None else None
    return {"m": (d_n ** 2 + d_e ** 2) ** 0.5, "n": d_n, "e": d_e, "alt": d_alt}


def derive_relative_positions(
    mapped: list[dict],
    array_origin: Optional[dict] = None,
) -> None:
    """Fill in `lat_lon` for all nodes that have a stored E/N/Alt position.
    Mutates the mapped dicts in place.

    `array_origin` is the hub-level geographic datum — a dict with keys
    `lat`, `lon`, `alt_m` from the `array_origin` DB table.  It is independent
    of any specific node; pass it in from routes._mapped_nodes().

    If `array_origin` is None (not yet configured), falls back to the
    hub-side GPS EMA of the first online BROKER node — a pre-survey display
    aid only, not used for TDOA.

    All nodes with a stored position_relative are projected from the datum
    the same way — there is no node that "is" the origin any more (the hub
    array origin is a standalone geographic datum, not tied to a node).
    """
    if array_origin is None:
        # Pre-survey fallback: use BROKER's hub-side GPS EMA as an
        # approximate datum.
        for m in mapped:
            if m.get("role") == "BROKER":
                gps = m.get("gps") or {}
                ema = gps.get("ema")
                if ema:
                    array_origin = {"lat": ema["lat"], "lon": ema["lon"]}
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
        # it so the UI shows that provenance rather than a GPS-EMA guess.
        m["flags"] = [*m.get("flags", []), "POSITION_DERIVED"]


def map_status(
    role: str,
    reachable: bool,
    raw_status: Optional[dict],
    node_pos: Optional[dict] = None,
    array_origin: Optional[dict] = None,
    gps_ema: Optional[dict] = None,
) -> dict:
    """Build the set of derived/display fields the frontend expects.

    `node_pos` is the hub's persisted position record for this node (from
    `db.get_node_position`), or None if no position has been set yet.

    `gps_ema` is this node's current hub-side GPS EMA (from
    `registry.get_gps_ema`), or None if no sample has been admitted yet.
    Replaces the firmware-reported `gpsCentroid`/`gpsEma` this function used
    to read out of `raw_status` — see the module docstring.

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
    lat_lon = None if position_known else _lat_lon_from_gps(raw_status, gps_ema)
    divergence = _ema_survey_divergence(node_pos, array_origin, gps_ema)

    return {
        "status": _status(reachable, flags),
        "role": _role(raw_status),
        "lat_lon": lat_lon,
        "position_relative": position_relative,
        "position_known": position_known,
        "position_status": pos_status,
        "is_origin": is_origin,
        "survey_disagreement_m": divergence["m"] if divergence else None,
        "gps": _gps(raw_status, gps_ema, divergence),
        "clock": clock,
        "audio": audio,
        "esp_now": None,
        "firmware_version": _node_info(raw_status).get("firmwareVersion"),
        "flags": flags,
    }
