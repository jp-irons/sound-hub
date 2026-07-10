"""
dawn_window_check.py — Compare real early-morning bird activity against the
current dawn-window buffers in server/suntimes.py (DAWN_BEFORE/DAWN_AFTER).

Motivation: on 2026-07-11 the user noticed bird calls at 05:53 local (13 min
before that day's dawn window opened at 06:06) that the Detections tab's
"Dawn" filter excluded. This script checks whether that's a one-off or a
consistent pattern, over the last N days, using two data sources:

  - detections     — BirdNET-confirmed species calls (what the Detections
                      tab's dawn filter actually uses)
  - trigger_events — raw on-node AudioTrigger fires (fired=1), more
                      sensitive than BirdNET since it doesn't need a
                      confident species ID — better signal for "when did
                      acoustic activity actually start"

For each of the last N local calendar dates it computes that date's
sunrise (via astral, same as server/suntimes.py) and the *current*
dawn window [sunrise - DAWN_BEFORE, sunrise + DAWN_AFTER], then finds the
earliest detection/trigger-fire inside [sunrise - pre_buffer, dawn_end].
The gap between that earliest activity and dawn_start tells you how much
(if any) real activity the current DAWN_BEFORE buffer is missing.

Run on the NUC (has the venv with astral/timezonefinder already installed
for the server):

    cd /opt/sound-hub
    venv/bin/python3 tools/dawn_window_check.py
    venv/bin/python3 tools/dawn_window_check.py --days 14 --min-conf 0.5
    venv/bin/python3 tools/dawn_window_check.py --db /opt/sound-hub/sound_hub.db

Uses stdlib sqlite3 + the server's own suntimes module (imported directly,
so the dawn-window math can never drift out of sync with what the app
actually uses) — no dependency on tools/venv.
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

# Make `server.suntimes` importable regardless of cwd — this file lives in
# <repo_root>/tools/, so the repo root is one level up.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from server.suntimes import DAWN_AFTER, DAWN_BEFORE, local_tz_for, windows_for_date  # noqa: E402

DEFAULT_DB_PATH = "/opt/sound-hub/sound_hub.db"


def _connect(db_path: str) -> sqlite3.Connection:
    path = os.path.abspath(db_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Database not found: {path}")
    return sqlite3.connect(path)


def _array_origin(conn: sqlite3.Connection) -> tuple[float, float]:
    row = conn.execute("SELECT lat, lon FROM array_origin WHERE id = 1").fetchone()
    if row is None:
        raise RuntimeError(
            "No array_origin set in this database — dawn/dusk windows can't "
            "be computed without a reference lat/lon. Set it via the hub UI "
            "first (or pass --lat/--lon)."
        )
    return row


def _parse_iso(s: str) -> datetime:
    ts = datetime.fromisoformat(s)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _fmt_local(ts: datetime, tz) -> str:
    return ts.astimezone(tz).strftime("%H:%M:%S")


def _fmt_gap(delta: timedelta) -> str:
    mins = delta.total_seconds() / 60
    sign = "-" if mins < 0 else "+"
    return f"{sign}{abs(mins):.1f} min"


def earliest_detection(conn, lo: datetime, hi: datetime, min_conf: float):
    row = conn.execute(
        """
        SELECT analyzed_at, common_name, confidence, node_id
        FROM detections
        WHERE analyzed_at >= ? AND analyzed_at <= ? AND confidence >= ?
        ORDER BY analyzed_at ASC
        LIMIT 1
        """,
        # analyzed_at is stored as UTC ISO8601 (see db.py schema comment); lo/hi
        # are local-tz-aware (from windows_for_date), so they must be converted
        # to UTC before comparison — SQLite does plain TEXT/lexicographic
        # comparison on this column, which silently breaks across mismatched
        # UTC offsets (e.g. "+10:00" vs "+00:00") even though both are valid
        # ISO8601 and represent the correct instant.
        (lo.astimezone(timezone.utc).isoformat(), hi.astimezone(timezone.utc).isoformat(), min_conf),
    ).fetchone()
    if row is None:
        return None
    analyzed_at, common_name, confidence, node_id = row
    return {
        "ts": _parse_iso(analyzed_at),
        "label": f"{common_name} ({confidence:.2f}) on {node_id or '?'}",
    }


def earliest_trigger(conn, lo: datetime, hi: datetime):
    lo_us, hi_us = int(lo.timestamp() * 1_000_000), int(hi.timestamp() * 1_000_000)
    row = conn.execute(
        """
        SELECT t_us, node_id, energy_ratio, flux_ratio
        FROM trigger_events
        WHERE t_us >= ? AND t_us <= ? AND fired = 1
        ORDER BY t_us ASC
        LIMIT 1
        """,
        (lo_us, hi_us),
    ).fetchone()
    if row is None:
        return None
    t_us, node_id, energy_ratio, flux_ratio = row
    return {
        "ts": datetime.fromtimestamp(t_us / 1_000_000, tz=timezone.utc),
        "label": f"fired on {node_id or '?'} (energy={energy_ratio:.1f}, flux={flux_ratio:.1f})",
    }


def main():
    parser = argparse.ArgumentParser(
        description="Check real early-morning bird activity against the current dawn window.")
    parser.add_argument("--db", default=DEFAULT_DB_PATH,
                        help=f"Path to sound_hub.db (default: {DEFAULT_DB_PATH})")
    parser.add_argument("--days", type=int, default=7,
                        help="Number of most-recent local calendar days to check (default: 7)")
    parser.add_argument("--pre-buffer-min", type=int, default=90,
                        help="How far before the CURRENT dawn_start to look for early activity, "
                             "in minutes (default: 90)")
    parser.add_argument("--min-conf", type=float, default=0.0,
                        help="Minimum BirdNET confidence for the detections source (default: 0.0, "
                             "matches the Detections tab's default)")
    parser.add_argument("--lat", type=float, default=None,
                        help="Override array_origin lat (skip DB lookup)")
    parser.add_argument("--lon", type=float, default=None,
                        help="Override array_origin lon (skip DB lookup)")
    args = parser.parse_args()

    conn = _connect(args.db)

    if args.lat is not None and args.lon is not None:
        lat, lon = args.lat, args.lon
    else:
        lat, lon = _array_origin(conn)

    local_tz = local_tz_for(lat, lon)
    print(f"Array origin: {lat:.5f}, {lon:.5f}  ({local_tz})")
    print(f"Current dawn window: sunrise - {DAWN_BEFORE} to sunrise + {DAWN_AFTER}\n")

    today_local = datetime.now(local_tz).date()
    pre_buffer = timedelta(minutes=args.pre_buffer_min)

    header = f"{'Date':<12}{'Sunrise':<10}{'DawnStart':<11}{'Earliest detection':<38}{'Gap':<12}{'Earliest trigger fire':<45}{'Gap':<10}"
    print(header)
    print("-" * len(header))

    gaps_min = []  # negative = activity found before dawn_start (i.e. window too tight)

    for i in range(args.days):
        d = today_local - timedelta(days=i)
        windows = windows_for_date(lat, lon, d, local_tz)
        dawn_start, dawn_end = windows["dawn"]

        lo = dawn_start - pre_buffer

        det = earliest_detection(conn, lo, dawn_end, args.min_conf)
        trig = earliest_trigger(conn, lo, dawn_end)

        det_str, det_gap_str = "—", ""
        if det:
            gap = det["ts"] - dawn_start
            det_str = f"{_fmt_local(det['ts'], local_tz)} {det['label']}"[:36]
            det_gap_str = _fmt_gap(gap)
            gaps_min.append(gap.total_seconds() / 60)

        trig_str, trig_gap_str = "—", ""
        if trig:
            gap = trig["ts"] - dawn_start
            trig_str = f"{_fmt_local(trig['ts'], local_tz)} {trig['label']}"[:43]
            trig_gap_str = _fmt_gap(gap)
            gaps_min.append(gap.total_seconds() / 60)

        print(
            f"{d.isoformat():<12}{_fmt_local(windows['dawn'][0] + DAWN_BEFORE, local_tz):<10}"
            f"{_fmt_local(dawn_start, local_tz):<11}{det_str:<38}{det_gap_str:<12}"
            f"{trig_str:<45}{trig_gap_str:<10}"
        )

    conn.close()

    print()
    if not gaps_min:
        print("No detections or trigger fires found in any window — nothing to conclude.")
        return

    earliest_gap = min(gaps_min)  # most negative = earliest relative to dawn_start
    avg_gap = sum(gaps_min) / len(gaps_min)
    print(f"Earliest activity relative to dawn_start across {args.days} day(s): "
          f"{_fmt_gap(timedelta(minutes=earliest_gap))} (avg {_fmt_gap(timedelta(minutes=avg_gap))})")

    if earliest_gap < 0:
        suggested = int((-earliest_gap + 4) // 5) * 5  # round up to nearest 5 min
        current = int(DAWN_BEFORE.total_seconds() // 60)
        print(f"Current DAWN_BEFORE = {current} min. Earliest activity seen was "
              f"{-earliest_gap:.1f} min before dawn_start.")
        if suggested > current:
            print(f"Consider DAWN_BEFORE ~= {suggested} min if this pattern holds across more days.")
    else:
        print("No activity found earlier than the current dawn_start — buffer looks adequate "
              "for this sample.")


if __name__ == "__main__":
    main()
