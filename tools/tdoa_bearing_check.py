#!/usr/bin/env python3
"""One-off diagnostic: for recent solved TDOA attempts, compute range and
compass bearing (from the node-array centroid) to the solved position, and
group repeat attempts of the same species to compare how tightly bearing
clusters versus how tightly range clusters.

Why: with nodes clustered ~10m apart (see project_bird_tdoa_baseline_
aperture_limit memory / the 2026-07-23 Monte Carlo sim against the real
solver), arrival-time differences barely change with range at real bird
distances -- bearing should stay comparatively well-determined while range
gets amplified noise. If repeat calls from what's plausibly the same real
perch show bearings clustering tightly while range scatters wildly (or
collapses toward the array), that's the aperture/GDOP limit showing up
exactly as predicted, not a correlation-quality bug. If bearing scatters
just as much as range, that points somewhere else (bad correlation picks,
wrong origin, etc.) and is worth chasing further.

Run on the hub VM:
    python3 tools/tdoa_bearing_check.py [N]     # N = how many recent solved attempts, default 30
"""
import sqlite3
import sys
import math
from collections import defaultdict

DB_PATH = "/opt/sound-hub/sound_hub.db"

n_attempts = int(sys.argv[1]) if len(sys.argv) > 1 else 30

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

positions = {
    row["node_id"]: (row["pos_e"], row["pos_n"], row["pos_alt"])
    for row in conn.execute(
        "SELECT node_id, pos_e, pos_n, pos_alt FROM node_positions "
        "WHERE pos_e IS NOT NULL AND pos_n IS NOT NULL AND pos_alt IS NOT NULL"
    )
}
if not positions:
    print("No positioned nodes found — nothing to compute a centroid from.")
    sys.exit(1)

cx = sum(p[0] for p in positions.values()) / len(positions)
cy = sum(p[1] for p in positions.values()) / len(positions)
print(f"Array centroid (from {len(positions)} positioned node(s)): "
      f"E={cx:.2f} N={cy:.2f}\n")


def bearing_deg(e: float, n: float) -> float:
    """Compass bearing (0=N, 90=E, ...) from the centroid to (e, n)."""
    return math.degrees(math.atan2(e - cx, n - cy)) % 360.0


def range_m(e: float, n: float) -> float:
    return math.hypot(e - cx, n - cy)


attempts = conn.execute(
    """
    SELECT id, created_at, species_key, origin_node_id,
           solved_e, solved_n, solved_alt, residual_m,
           uncorrelated_node_count
    FROM tdoa_attempts
    WHERE status = 'solved'
      AND solved_e IS NOT NULL AND solved_n IS NOT NULL
    ORDER BY id DESC
    LIMIT ?
    """,
    (n_attempts,),
).fetchall()

by_species = defaultdict(list)

print(f"{'id':>5}  {'created_at':<20} {'species':<22} {'origin':<16} "
      f"{'range_m':>8} {'bearing':>8} {'residual':>9} {'uncorr':>6}")
for a in attempts:
    r = range_m(a["solved_e"], a["solved_n"])
    b = bearing_deg(a["solved_e"], a["solved_n"])
    print(f"{a['id']:>5}  {a['created_at']:<20} {a['species_key']:<22} "
          f"{a['origin_node_id']:<16} {r:8.2f} {b:8.1f} "
          f"{a['residual_m']:9.3f} {a['uncorrelated_node_count'] if a['uncorrelated_node_count'] is not None else '?':>6}")
    by_species[a["species_key"]].append((a["id"], r, b))

print("\n--- per-species spread (bearing tight + range wide = aperture/GDOP limit as predicted) ---")
for species, rows in by_species.items():
    if len(rows) < 2:
        continue
    ranges = [r for _, r, _ in rows]
    bearings = [b for _, _, b in rows]
    # Circular spread for bearing: min great-circle-style spread across the
    # 0/360 wrap, cheap approximation via sin/cos mean vector length (R=1
    # means all bearings identical, R=0 means uniformly scattered).
    mean_sin = sum(math.sin(math.radians(b)) for b in bearings) / len(bearings)
    mean_cos = sum(math.cos(math.radians(b)) for b in bearings) / len(bearings)
    resultant_length = math.hypot(mean_sin, mean_cos)  # 1.0 = identical bearings
    print(f"{species:<22} n={len(rows):<3} "
          f"range: min={min(ranges):7.2f} max={max(ranges):7.2f} spread={max(ranges)-min(ranges):7.2f}   "
          f"bearing concentration R={resultant_length:.2f} (1.0=identical, 0=scattered)")

conn.close()
