#!/usr/bin/env python3
"""One-off diagnostic: for recent solved TDOA attempts, re-solve using ONLY
the origin plus 'trusted' corroborators (dropping 'untrusted' ones), and
compare against the original all-arrived-nodes solve that's actually live in
production. Read-only -- does not touch the DB or change solver behaviour.

Why: task #18 (restrict _maybe_solve_tdoa_attempt_inner's solver input to
correlation_status=='trusted' rows) has been deferred all session pending
real data -- see project_soundhub_onset_gating_removed /
project_soundhub_correlation_ambiguity_bound memories. Restricting the
solver means fewer attempts have enough nodes to solve at all (best real
case seen so far is 3/4 corroborators trusted, never 4/4), so it's worth
knowing whether trusted-only actually produces less implausible positions
before paying that cost live. This script answers that directly: it reuses
the real tdoa_solver.solve() (imported, not reimplemented) against both node
sets for the same historical attempts and reports both, plus range/bearing
from the array centroid for each (see tdoa_bearing_check.py -- same
aperture/GDOP reasoning applies: if trusted-only solves land noticeably
farther from the near-field-collapse pattern than the all-nodes solve did,
that's real evidence #18 is worth deploying).

The origin's own row is always included regardless of correlation_status --
it structurally can never be 'trusted' (never correlated against itself,
see project_soundhub_correlation_visibility's 2026-07-29 follow-up on the
uncorrelated_node_count off-by-one this same reasoning fixed) but it's the
reference every other timestamp is measured against, not a fallback value.

Run on the hub VM:
    python3 tools/tdoa_trusted_only_dryrun.py [N]   # N = how many recent solved attempts, default 30
"""
import math
import os
import sqlite3
import sys

SERVER_DIR = os.path.join(os.path.dirname(__file__), "..", "server")
sys.path.insert(0, SERVER_DIR)
from tdoa_solver import solve, Node, DEFAULT_SPEED_OF_SOUND  # noqa: E402

DB_PATH = "/opt/sound-hub/sound_hub.db"

n_attempts = int(sys.argv[1]) if len(sys.argv) > 1 else 30

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

positions = {
    row["node_id"]: (row["pos_e"], row["pos_n"], row["pos_alt"])
    for row in conn.execute(
        # Same offline approximation of _approved_positioned_nodes() as
        # tdoa_bearing_check.py -- see that script's comment on why isBroker
        # can't be replicated from the DB alone.
        """SELECT p.node_id, p.pos_e, p.pos_n, p.pos_alt
           FROM node_positions p
           JOIN nodes n ON n.id = p.node_id
           WHERE p.pos_e IS NOT NULL AND p.pos_n IS NOT NULL AND p.pos_alt IS NOT NULL
             AND n.approval_status = 'approved'"""
    )
}
if not positions:
    print("No positioned nodes found — nothing to compute a centroid from.")
    sys.exit(1)

cx = sum(p[0] for p in positions.values()) / len(positions)
cy = sum(p[1] for p in positions.values()) / len(positions)
print(f"Array centroid (from {len(positions)} positioned node(s)): E={cx:.2f} N={cy:.2f}\n")


def bearing_deg(e: float, n: float) -> float:
    return math.degrees(math.atan2(e - cx, n - cy)) % 360.0


def range_m(e: float, n: float) -> float:
    return math.hypot(e - cx, n - cy)


def try_solve(node_rows: list) -> dict | None:
    """node_rows: list of (node_id, arrival_us). Returns solve dict or None
    if fewer than 4 usable (positioned) nodes."""
    usable = [(nid, us) for nid, us in node_rows if nid in positions]
    if len(usable) < 4:
        return None
    solver_nodes = [
        Node(node_id=nid, x=positions[nid][0], y=positions[nid][1], z=positions[nid][2])
        for nid, _ in usable
    ]
    timestamps = [us for _, us in usable]
    ccx = sum(n.x for n in solver_nodes) / len(solver_nodes)
    ccy = sum(n.y for n in solver_nodes) / len(solver_nodes)
    ccz = sum(n.z for n in solver_nodes) / len(solver_nodes)
    try:
        result = solve(
            nodes=solver_nodes, timestamps_us=timestamps,
            speed_of_sound=DEFAULT_SPEED_OF_SOUND, hint_point=(ccx, ccy, ccz),
        )
    except ValueError as e:
        return {"error": str(e), "n_nodes": len(usable)}
    return {
        "n_nodes": len(usable), "x": result.x, "y": result.y, "z": result.z,
        "residual": result.residual, "method": result.method,
    }


attempts = conn.execute(
    """
    SELECT id, created_at, species_key, origin_node_id,
           solved_e, solved_n, solved_alt, solve_residual_m
    FROM tdoa_attempts
    WHERE status = 'solved'
      AND solved_e IS NOT NULL AND solved_n IS NOT NULL
    ORDER BY id DESC
    LIMIT ?
    """,
    (n_attempts,),
).fetchall()

n_could_trusted_solve = 0
n_total = 0

for a in attempts:
    nodes = conn.execute(
        """SELECT node_id, arrival_us, correlation_status
           FROM tdoa_attempt_nodes
           WHERE attempt_id = ? AND status = 'arrived' AND arrival_us IS NOT NULL""",
        (a["id"],),
    ).fetchall()

    all_rows = [(n["node_id"], n["arrival_us"]) for n in nodes]
    trusted_rows = [
        (n["node_id"], n["arrival_us"]) for n in nodes
        if n["node_id"] == a["origin_node_id"] or n["correlation_status"] == "trusted"
    ]
    n_trusted_corroborators = sum(
        1 for n in nodes
        if n["node_id"] != a["origin_node_id"] and n["correlation_status"] == "trusted"
    )

    n_total += 1
    orig_range = range_m(a["solved_e"], a["solved_n"])
    orig_bearing = bearing_deg(a["solved_e"], a["solved_n"])

    print(f"=== attempt {a['id']} ({a['created_at']}) species={a['species_key']} "
          f"origin={a['origin_node_id']} ===")
    print(f"  ALL nodes  ({len(all_rows)}):     range={orig_range:6.2f}m  "
          f"bearing={orig_bearing:6.1f}  residual={a['solve_residual_m']:.3f}m")

    trusted_result = try_solve(trusted_rows)
    if trusted_result is None:
        print(f"  TRUSTED-only ({len(trusted_rows)}, {n_trusted_corroborators} trusted corroborator(s)): "
              f"fewer than 4 usable nodes -- would NOT solve under #18")
    elif "error" in trusted_result:
        print(f"  TRUSTED-only ({trusted_result['n_nodes']}): solver error -- {trusted_result['error']}")
    else:
        t_range = range_m(trusted_result["x"], trusted_result["y"])
        t_bearing = bearing_deg(trusted_result["x"], trusted_result["y"])
        shift_m = math.hypot(trusted_result["x"] - a["solved_e"], trusted_result["y"] - a["solved_n"])
        n_could_trusted_solve += 1
        print(f"  TRUSTED-only ({trusted_result['n_nodes']}, {n_trusted_corroborators} trusted corroborator(s), "
              f"method={trusted_result['method']}): range={t_range:6.2f}m  bearing={t_bearing:6.1f}  "
              f"residual={trusted_result['residual']:.3f}m  |  shift from all-nodes solve: {shift_m:.2f}m")
    print()

print(f"--- summary ---")
print(f"{n_total} solved attempt(s) checked; {n_could_trusted_solve} "
      f"({100*n_could_trusted_solve/n_total:.0f}%) would still have enough trusted nodes to solve under #18.")
print("Eyeball the per-attempt 'shift from all-nodes solve' and whether trusted-only range/bearing "
      "look less like the near-field-collapse pattern (see project_bird_tdoa_baseline_aperture_limit "
      "memory) than the original solve did.")
