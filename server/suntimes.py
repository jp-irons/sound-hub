"""Sun-relative time-of-day classification (dawn / dusk / daytime / nighttime).

Uses the array_origin lat/lon (the same reference datum already used for
cartesian node-position math — see db.get_array_origin()) rather than
per-node GPS. One sun calculation per calendar date covers the whole array
regardless of how many nodes exist or where they're sited.

The local timezone is derived from the array_origin lat/lon itself (via
timezonefinder), not hardcoded to any one site. This setup is designed to be
portable — e.g. a second hub in a van, re-surveyed at each new campsite —
so the location, and therefore the local day boundary used for sunrise/
sunset classification, has to follow array_origin rather than being baked
in for a single fixed property.

Each detection timestamp is classified using the sunrise/sunset of *its own*
local calendar date. This sidesteps any need to reason about windows that
cross midnight: a 2am detection is classified against that same local date's
(later, same-day) sunrise and is correctly "nighttime" because it falls
before that date's dawn window — there's no need to look at the previous
day's dusk window at all.

Note on timezone vs. UTC: computing sun() in UTC for a location far from the
prime meridian returns sunrise/sunset assigned to mismatched UTC calendar
days (UTC midnight can fall mid-afternoon local time), which silently
produces nonsensical dawn/dusk window ordering. Always resolve the local
zone for the array's actual lat/lon first.
"""

from __future__ import annotations

from datetime import date as date_, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun
from timezonefinder import TimezoneFinder

# TimezoneFinder loads a sizeable boundary dataset on construction — build it
# once per process, not per call.
_tf = TimezoneFinder()


def local_tz_for(lat: float, lon: float) -> ZoneInfo:
    """Resolve the IANA timezone for a lat/lon, with a fixed-offset fallback
    for the rare case timezonefinder can't place it (e.g. open ocean)."""
    name = _tf.timezone_at(lat=lat, lng=lon)
    if name is None:
        # Longitude-based fixed-offset fallback — coarse (15-degree bands)
        # but keeps local-day classification sane even with no IANA match.
        offset_hours = round(lon / 15)
        return timezone(timedelta(hours=offset_hours))
    return ZoneInfo(name)


# Buffers around actual sunrise/sunset.
#
# Dawn chorus activity tends to start a little before sunrise (as light
# increases) and build for an hour or more afterwards as birds disperse to
# forage — hence a window weighted *after* sunrise.
#
# The evening chorus tends to be shorter and concentrated in the run-up to
# sunset, tailing off quickly once it's dark — hence a window weighted
# *before* sunset. Adjust these once you have enough real detection data
# to see where the actual chorus boundaries fall at a given site.
DAWN_BEFORE = timedelta(minutes=45)
DAWN_AFTER = timedelta(minutes=90)
DUSK_BEFORE = timedelta(minutes=90)
DUSK_AFTER = timedelta(minutes=30)

TIME_OF_DAY_VALUES = ("dawn", "dusk", "daytime", "nighttime")


def _sun_times(lat: float, lon: float, on_date: date_, local_tz: ZoneInfo) -> dict:
    loc = LocationInfo(latitude=lat, longitude=lon)
    return sun(loc.observer, date=on_date, tzinfo=local_tz)


def windows_for_date(
    lat: float, lon: float, on_date: date_, local_tz: ZoneInfo | None = None,
) -> dict[str, tuple[datetime, datetime]]:
    """Return the dawn/dusk/daytime/nighttime window boundaries for one date.

    nighttime is split either side of the day (before dawn, after dusk) —
    callers checking nighttime should treat membership in *either* sub-range
    as a match (see classify()), rather than relying on this dict directly.

    Pass local_tz if already resolved (see classify_many) to avoid repeating
    the timezone lookup.
    """
    if local_tz is None:
        local_tz = local_tz_for(lat, lon)
    s = _sun_times(lat, lon, on_date, local_tz)
    sunrise, sunset = s["sunrise"], s["sunset"]

    dawn_start, dawn_end = sunrise - DAWN_BEFORE, sunrise + DAWN_AFTER
    dusk_start, dusk_end = sunset - DUSK_BEFORE, sunset + DUSK_AFTER

    return {
        "dawn": (dawn_start, dawn_end),
        "daytime": (dawn_end, dusk_start),
        "dusk": (dusk_start, dusk_end),
        # two disjoint pieces of the same calendar date, before dawn / after dusk
        "nighttime": (dusk_end, dawn_start),
    }


def _bucket(ts: datetime, lat: float, lon: float, local_tz: ZoneInfo) -> str:
    local_date = ts.astimezone(local_tz).date()
    w = windows_for_date(lat, lon, local_date, local_tz)

    if w["dawn"][0] <= ts <= w["dawn"][1]:
        return "dawn"
    if w["dusk"][0] <= ts <= w["dusk"][1]:
        return "dusk"
    if w["daytime"][0] < ts < w["daytime"][1]:
        return "daytime"
    return "nighttime"


def classify(ts: datetime, lat: float, lon: float) -> str:
    """Classify a single timestamp into dawn/dusk/daytime/nighttime, anchored
    to its own local calendar date's sunrise/sunset (see module docstring).

    For classifying many timestamps against the same lat/lon (e.g. a page of
    detection rows), prefer classify_many() — this resolves the timezone
    fresh on every call.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return _bucket(ts, lat, lon, local_tz_for(lat, lon))


def classify_many(timestamps, lat: float, lon: float) -> list[str]:
    """Classify multiple timestamps against the same lat/lon, resolving the
    timezone lookup once instead of once per timestamp."""
    local_tz = local_tz_for(lat, lon)
    out = []
    for ts in timestamps:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append(_bucket(ts, lat, lon, local_tz))
    return out
