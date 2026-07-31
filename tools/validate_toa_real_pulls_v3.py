"""
validate_toa_real_pulls_v3.py — geometric self-consistency comparison of
raw/gcc_phat/phat_masked against production ('plain'), on the same 6 real
DB-backed attempts used by validate_toa_real_pulls_v2.py.

WHY THIS EXISTS (2026-07-31): v2 scored each candidate method by its
distance from production's own real, stored arrival_us for each node. Jon
correctly flagged that as circular — production's own output is exactly the
thing under question (that's the whole reason for this investigation), so
"how far is the candidate from production" can only ever measure agreement
with the status quo, not correctness. It also can't be rescued by pointing
at status='solved'/'arrived': this project has repeatedly found "trusted"/
"solved" results that were later shown to be wrong (see
project_soundhub_correlation_ambiguity_bound / project_soundhub_
timestamp_precision_bug memories), so a 'solved' stamp isn't independent
confirmation that production's own value was right either.

THIS SCRIPT instead uses a reference-free correctness proxy: geometric
self-consistency. Real, surveyed node positions plus a real point source
impose a hard physical constraint — a genuinely correct set of arrival
times across N nodes must satisfy the same range equations that
tdoa_solver.solve() already uses in production, with a small residual. That
constraint doesn't care which correlation method produced the arrival
times, or what production output for this same event; it only checks
whether the times are mutually consistent with SOME single point source
given real geometry. So for each of the 6 real attempts, this script builds
FOUR competing arrival-time sets from the SAME real audio, SAME real node
geometry, and SAME real origin onset time (only the neighbour-to-origin
correlation step is swapped):

  - production : production's own real, stored arrival_us per node
  - raw        : this repo's reproduction of 'plain' correlation (should
                 track 'production' closely — see v2's sanity-check note;
                 included here mainly as an internal consistency check that
                 this script's own solve plumbing is faithful)
  - gcc_phat   : today's actual (buggy — filter-then-whiten) production
                 gcc_phat branch
  - phat_masked: Jon's proposed fix (whiten-then-mask)

...then feeds each set through the real tdoa_solver.solve() and compares
the resulting RMS range residual (metres). Lower residual = more
geometrically self-consistent = more likely correct, independent of
whether it agrees with production.

Only the neighbour-to-origin lag is swapped per method; origin_arrival_us
itself comes from onset detection (shared infrastructure, not part of what
this investigation is testing) and is held fixed across all four sets.

CAVEAT: all 6 attempts here have status='solved' with every node
status='arrived' — i.e. these are cases production's own pipeline already
succeeded at by its own gates, not examples of the "mostly untrusted"
failure mode that originally motivated this investigation. A method that
wins on self-consistency here demonstrates it doesn't regress on
production's easy cases; it does NOT by itself demonstrate it would do
better on production's hard/failing cases. That would need the same
residual comparison run against untrusted/unsolved attempts instead.

Report-only: does not modify server/correlation.py or server/
tdoa_solver.py, does not write to the live DB.

Usage:
    python tools/validate_toa_real_pulls_v3.py
    python tools/validate_toa_real_pulls_v3.py --json-out real_pulls_v3_report.json

Requires: numpy, scipy, soundfile. Run from the sound-hub root directory.
"""

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from tdoa_solver import Node, solve  # noqa: E402

from validate_toa_real_pulls import _load_node_positions  # noqa: E402
from validate_toa_real_pulls_v2 import (  # noqa: E402
    DEFAULT_AUDIO_DIR, DEFAULT_EXTRACT_DB,
    _analyze_real_attempt, _load_attempts,
)

METHODS = ("production", "raw", "gcc_phat", "phat_masked")

# node_positions has soundcapture170 at (0,0,0) -- the array's reference
# origin, i.e. "the deck" the project's own accuracy targets (1m within
# 50m of deck, 5m within 150m) are stated relative to. Used below to judge
# whether a solved position is a *believable* real call site, independent
# of whether it's accurate: a "solve" that lands on top of the sensor
# cluster itself, or far outside the ~3Ha property, isn't a plausible bird
# location either way, regardless of how low its residual is.
DECK_ORIGIN = np.array([0.0, 0.0, 0.0])

# Nodes are currently clustered within ~0-6m of the deck (see
# project_bird_tdoa_baseline_aperture_limit memory) -- a solved position
# this close to the deck is effectively "inside/on the sensor cluster",
# not a real canopy call site.
NEAR_ARRAY_FLAG_M = 5.0
# Property is ~3Ha (order ~150-200m across) -- a solve well past that is
# off the property and implausible regardless of residual.
FAR_FLAG_M = 200.0


def _solve_geometry(positions: dict, origin_node: str, origin_arrival_us: float,
                     node_rows: list, method: str):
    """Build the arrival-time set this method implies for one attempt (same
    real geometry, same real origin onset time, method-specific neighbour
    lags) and solve for position + RMS range residual via the real
    production solver.

    Returns (SolveResult|None, n_nodes_used, error) — error is None on
    success, else a short string explaining why this method couldn't be
    solved for this attempt (too few positioned nodes, or solve() itself
    rejected the geometry/timestamps).
    """
    if origin_node not in positions:
        return None, 0, f"no surveyed position for origin {origin_node}"

    nodes = [Node(node_id=origin_node, x=positions[origin_node][0],
                  y=positions[origin_node][1], z=positions[origin_node][2])]
    timestamps = [origin_arrival_us]

    for row in node_rows:
        node_id = row["node"]
        if node_id not in positions:
            continue
        if method == "production":
            t = row["real_arrival_us"]
        else:
            t = origin_arrival_us + row[method]["lag_us"]
        nodes.append(Node(node_id=node_id, x=positions[node_id][0],
                           y=positions[node_id][1], z=positions[node_id][2]))
        timestamps.append(t)

    if len(nodes) < 4:
        return None, len(nodes), f"only {len(nodes)} positioned node(s), need >=4"

    try:
        result = solve(nodes, timestamps)
    except ValueError as e:
        return None, len(nodes), str(e)
    return result, len(nodes), None


def _believability_flag(dist_deck_m: float) -> str:
    if dist_deck_m < NEAR_ARRAY_FLAG_M:
        return " [FLAG: inside/on the sensor cluster -- not a believable canopy call site]"
    if dist_deck_m > FAR_FLAG_M:
        return " [FLAG: off-property -- implausibly far for a 3Ha site]"
    return ""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audio-dir", default=DEFAULT_AUDIO_DIR)
    parser.add_argument("--extract-db", default=DEFAULT_EXTRACT_DB)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    positions = _load_node_positions(args.extract_db)
    attempts, nodes_by_attempt = _load_attempts(args.extract_db)
    print(f"Loaded {len(attempts)} attempt(s), {len(positions)} node position(s)")

    full_report = {}
    residuals_by_method = {m: [] for m in METHODS}
    dist_deck_by_method = {m: [] for m in METHODS}
    wins = {m: 0 for m in METHODS}
    believable_counts = {m: {"believable": 0, "total": 0} for m in METHODS}
    comparable_attempts = 0

    for attempt_id in sorted(attempts):
        attempt = attempts[attempt_id]
        print(f"\n=== attempt {attempt_id} ({attempt['species_key']}) ===")
        result = _analyze_real_attempt(attempt, nodes_by_attempt.get(attempt_id, []), args.audio_dir, positions)
        if result is None:
            continue

        geom_node_ids = [nid for nid in ([result["origin_node"]] + [r["node"] for r in result["nodes"]])
                          if nid in positions]
        centroid = np.mean([positions[nid] for nid in geom_node_ids], axis=0) if geom_node_ids else None

        stored_residual = attempt.get("solve_residual_m")
        stored_e, stored_n, stored_alt = attempt.get("solved_e"), attempt.get("solved_n"), attempt.get("solved_alt")
        row_results = {}
        for m in METHODS:
            geom_result, n_nodes, err = _solve_geometry(
                positions, result["origin_node"], result["origin_arrival_us"], result["nodes"], m)
            row_results[m] = {"geom_result": geom_result, "n_nodes": n_nodes, "error": err}

        print(f"  --- solved position (x=E,y=N,z=Alt metres) + distance from deck (0,0,0) + residual ---")
        if stored_residual is not None:
            stored_dist_deck = (
                float(np.linalg.norm([stored_e, stored_n, stored_alt])) if stored_e is not None else None
            )
            dd_str = f", dist_from_deck={stored_dist_deck:.2f}m" if stored_dist_deck is not None else ""
            print(f"      (production's live stored solve: residual={stored_residual:.3f}m{dd_str}"
                  f" — may use a different node subset than the reproduction below)")
        for m in METHODS:
            r = row_results[m]
            gr = r["geom_result"]
            if gr is None:
                print(f"      {m:12s}: FAILED ({r['n_nodes']} nodes) — {r['error']}")
                continue
            dist_deck = float(np.linalg.norm([gr.x, gr.y, gr.z] - DECK_ORIGIN))
            dist_centroid = float(np.linalg.norm(np.array([gr.x, gr.y, gr.z]) - centroid)) if centroid is not None else float("nan")
            flag = _believability_flag(dist_deck)
            believable_counts[m]["total"] += 1
            if not flag:
                believable_counts[m]["believable"] += 1
            print(f"      {m:12s}: pos=({gr.x:+7.2f},{gr.y:+7.2f},{gr.z:+7.2f})  "
                  f"dist_from_deck={dist_deck:7.2f}m  dist_from_centroid={dist_centroid:6.2f}m  "
                  f"residual={gr.residual:6.3f}m  ({r['n_nodes']} nodes){flag}")

        valid = {m: row_results[m]["geom_result"] for m in METHODS if row_results[m]["geom_result"] is not None}
        if len(valid) == len(METHODS):
            comparable_attempts += 1
            best = min(valid, key=lambda m: valid[m].residual)
            wins[best] += 1
            print(f"      -> lowest residual: {best}")
            for m in METHODS:
                residuals_by_method[m].append(valid[m].residual)
                dist_deck_by_method[m].append(float(np.linalg.norm([valid[m].x, valid[m].y, valid[m].z] - DECK_ORIGIN)))

        full_report[attempt_id] = {
            "species": attempt["species_key"], "origin_node": result["origin_node"],
            "stored_solve_residual_m": stored_residual,
            "methods": {
                m: {
                    "n_nodes": row_results[m]["n_nodes"], "error": row_results[m]["error"],
                    **({"x": row_results[m]["geom_result"].x, "y": row_results[m]["geom_result"].y,
                        "z": row_results[m]["geom_result"].z, "residual_m": row_results[m]["geom_result"].residual}
                       if row_results[m]["geom_result"] is not None else {}),
                }
                for m in METHODS
            },
        }

    print(f"\n=== Summary (attempts where all {len(METHODS)} methods solved: {comparable_attempts}/{len(attempts)}) ===")
    for m in METHODS:
        arr = np.array(residuals_by_method[m])
        dd = np.array(dist_deck_by_method[m])
        bc = believable_counts[m]
        pct_believable = (100.0 * bc["believable"] / bc["total"]) if bc["total"] else 0.0
        if len(arr) == 0:
            print(f"  {m:12s}: no comparable data")
            continue
        print(f"  {m:12s}: residual mean={np.mean(arr):6.3f}m median={np.median(arr):6.3f}m  |  "
              f"dist_from_deck mean={np.mean(dd):6.2f}m median={np.median(dd):6.2f}m  |  "
              f"lowest-residual-wins={wins[m]}/{comparable_attempts}  |  "
              f"believable={bc['believable']}/{bc['total']} ({pct_believable:.0f}%)")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(full_report, f, indent=2, default=str)
        print(f"\nWrote full report to {args.json_out}")

    print("\nSelf-consistency check only — no method's own arrival_us is treated as ground truth;\n"
          "residual measures whether each method's arrival-time set is physically consistent with a\n"
          "single point source given real surveyed geometry; dist_from_deck/believable flag whether\n"
          "that position is even a plausible real call site, independent of residual. See module\n"
          "docstring for the caveat that all 6 attempts here are ones production already solved/\n"
          "trusted — a win here shows 'doesn't regress on production's easy cases', not 'fixes\n"
          "production's hard/failing cases'.")


if __name__ == "__main__":
    main()
