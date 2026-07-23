#!/usr/bin/env python3
"""One-off diagnostic: for recent TDOA attempts, check whether the
TDOA-implied range difference between each node pair exceeds the physical
baseline distance between them. This is impossible for a real source at any
distance/direction (max possible delay between two receivers is exactly the
time sound takes to travel the straight line between them) -- so if we see
violations, that's not "far away source", it's a unit/scale/clock bug
somewhere in the arrival_us or pos_e/pos_n/pos_alt pipeline.

Run on the hub VM:
    python3 tools/tdoa_scale_check.py [N]     # N = how many recent attempts, default 8
"""
import sqlite3
import sys
import math

DB_PATH = "/opt/sound-hub/sound_hub.db"
C = 343.0  # m/s, matches tdoa_solver.DEFAULT_SPEED_OF_SOUND

n_attempts = int(sys.argv[1]) if len(sys.argv) > 1 else 8

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

attempts = conn.execute(
    """
    SELECT id, status, species_key, failure_reason, solved_e, solved_n, solved_alt
    FROM tdoa_attempts
    WHERE status IN ('solved', 'failed')
    ORDER BY id DESC
    LIMIT ?
    """,
    (n_attempts,),
).fetchall()

positions = {
    row["node_id"]: (row["pos_e"], row["pos_n"], row["pos_alt"])
    for row in conn.execute(
        "SELECT node_id, pos_e, pos_n, pos_alt FROM node_positions "
        "WHERE pos_e IS NOT NULL AND pos_n IS NOT NULL AND pos_alt IS NOT NULL"
    )
}

for att in attempts:
    nodes = conn.execute(
        """
        SELECT node_id, arrival_us
        FROM tdoa_attempt_nodes
        WHERE attempt_id = ? AND status = 'arrived' AND arrival_us IS NOT NULL
        ORDER BY node_id
        """,
        (att["id"],),
    ).fetchall()

    usable = [n for n in nodes if n["node_id"] in positions]
    if len(usable) < 2:
        continue

    print(f"\n=== attempt {att['id']} status={att['status']} species={att['species_key']}"
          + (f" reason={att['failure_reason']}" if att["failure_reason"] else "")
          + (f" solved=({att['solved_e']:.1f},{att['solved_n']:.1f},{att['solved_alt']:.1f})"
             if att["solved_e"] is not None else ""))

    worst_ratio = 0.0
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            ni, nj = usable[i], usable[j]
            xi, yi, zi = positions[ni["node_id"]]
            xj, yj, zj = positions[nj["node_id"]]
            baseline_m = math.sqrt((xi - xj) ** 2 + (yi - yj) ** 2 + (zi - zj) ** 2)
            dt_us = ni["arrival_us"] - nj["arrival_us"]
            implied_range_diff_m = C * dt_us * 1e-6
            ratio = abs(implied_range_diff_m) / baseline_m if baseline_m > 0 else float("inf")
            worst_ratio = max(worst_ratio, ratio)
            flag = "  <-- IMPOSSIBLE (exceeds baseline)" if ratio > 1.0 else ""
            print(f"  {ni['node_id']:>16} - {nj['node_id']:<16} "
                  f"baseline={baseline_m:7.2f}m  dt={dt_us:10.1f}us  "
                  f"implied_diff={implied_range_diff_m:9.2f}m  ratio={ratio:5.2f}{flag}")

    print(f"  worst pairwise ratio: {worst_ratio:.2f}"
          + ("  <-- physically impossible, points at a unit/scale/clock bug"
             if worst_ratio > 1.0 else "  (within physical bounds)"))

conn.close()
