"""
tdoa_solver_synthetic_check.py — isolates tdoa_solver.solve() from any TOA/
correlation questions entirely, using clean synthetic (exactly-known)
arrival times computed from the REAL surveyed node geometry (sound_hub_
extract2.db: soundcapture170-174) and a known true source position.

WHY (2026-07-31): validate_toa_real_pulls_v3.py found every real solved
attempt — regardless of correlation method (production/raw/gcc_phat/
phat_masked) — collapses to within a few metres of the deck, no matter
what the true call distance presumably was. That's not the pattern plain
GDOP-amplified timing noise should produce on its own (growing SCATTER
with distance, per the earlier Monte Carlo in project_bird_tdoa_baseline_
aperture_limit) — it looked like a consistent pull toward the array. Since
all 4 real-data methods showed the same collapse, the correlation method
may not be the (or the only) driver; this script isolates the SOLVER
itself, with exactly-known inputs, to find out whether that's a solver-side
effect or something the TOA/correlation stage is doing to the inputs.

Note: with 5 real nodes, tdoa_solver.solve() always takes the
least_squares path (n>=5) — the quadratic 4-node dual-root/centroid-
preference heuristic in _select_root never runs for this array's real
solves, so that heuristic specifically cannot be the explanation for any
bias found here.

Method:
  Step 1 — noiseless sanity check: for a grid of true source distances/
  bearings around the real array (fixed height above the deck), compute
  EXACT arrival times at each real node (known geometry, known
  DEFAULT_SPEED_OF_SOUND) and solve. solve() should recover the true
  position to within floating-point precision; if it doesn't, that alone
  is a solver bug independent of any noise or correlation question.

  Step 2 — noise sweep: repeat with Gaussian timing noise added at several
  sigma levels spanning the disagreement scales actually seen between
  correlation methods on real data (tens of microseconds to several
  milliseconds), many trials per (distance, noise) combination, averaged
  across bearings. Reports both error MAGNITUDE (precision) and whether
  solved positions are systematically closer to the deck than the true
  source on average (a real inward BIAS, not just scatter).

Report-only: does not modify server/tdoa_solver.py, no DB writes.

Usage:
    python tools/tdoa_solver_synthetic_check.py
    python tools/tdoa_solver_synthetic_check.py --extract-db sound_hub_extract2.db --trials 300

Requires: numpy. Run from the sound-hub root directory.
"""

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from tdoa_solver import DEFAULT_SPEED_OF_SOUND, Node, solve  # noqa: E402

from validate_toa_real_pulls import _load_node_positions  # noqa: E402

DEFAULT_EXTRACT_DB = os.path.join(os.path.dirname(__file__), "..", "sound_hub_extract2.db")

# The 5 real nodes involved in the attempts under investigation.
REAL_NODE_IDS = ["soundcapture170", "soundcapture171", "soundcapture172", "soundcapture173", "soundcapture174"]

# soundcapture170 is (0,0,0) in node_positions -- the array's reference
# origin ("the deck"). Used as the fixed anchor for both placing synthetic
# true sources and measuring inward bias, to match validate_toa_real_
# pulls_v3.py's dist_from_deck convention exactly.
DECK_ORIGIN = np.array([0.0, 0.0, 0.0])

TEST_DISTANCES_M = [5, 10, 20, 30, 50, 75, 100, 150]
TEST_BEARINGS_DEG = [0, 45, 90, 135, 180, 225, 270, 315]
TEST_ALTITUDE_M = 5.0  # canopy-ish height above deck level

# Spans the real disagreement scale seen between correlation methods on
# real attempts in validate_toa_real_pulls_v3.py (tens of us up to ~35ms).
NOISE_SIGMAS_US = [0.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0]


def _load_real_nodes(db_path: str) -> list:
    positions = _load_node_positions(db_path)
    nodes = []
    for nid in REAL_NODE_IDS:
        if nid not in positions:
            print(f"[warn] {nid} missing from {db_path}, skipping")
            continue
        pos_e, pos_n, pos_alt = positions[nid]
        nodes.append(Node(node_id=nid, x=pos_e, y=pos_n, z=pos_alt))
    return nodes


def _true_arrival_us(nodes: list, true_xyz: np.ndarray,
                      speed_of_sound: float = DEFAULT_SPEED_OF_SOUND) -> np.ndarray:
    """Exact (noiseless) arrival time at each node for a source emitting at
    t=0, in microseconds. Absolute epoch offset doesn't matter -- solve()
    internally anchors on min(timestamps_us) regardless (see tdoa_solver.py
    solve()'s t_ref comment)."""
    out = []
    for n in nodes:
        dist = np.sqrt((n.x - true_xyz[0]) ** 2 + (n.y - true_xyz[1]) ** 2 + (n.z - true_xyz[2]) ** 2)
        out.append(dist / speed_of_sound * 1e6)
    return np.array(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--extract-db", default=DEFAULT_EXTRACT_DB)
    parser.add_argument("--trials", type=int, default=200, help="total trials per (distance, noise) combo, split across bearings")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    nodes = _load_real_nodes(args.extract_db)
    if len(nodes) < 4:
        print(f"Need >=4 real node positions, got {len(nodes)}. Aborting.")
        return
    print(f"Loaded {len(nodes)} real node(s): {[n.node_id for n in nodes]}")
    centroid = np.mean([[n.x, n.y, n.z] for n in nodes], axis=0)
    print(f"Array centroid: ({centroid[0]:+.2f},{centroid[1]:+.2f},{centroid[2]:+.2f}), "
          f"deck at (0,0,0), centroid dist_from_deck={np.linalg.norm(centroid):.2f}m")

    # --- Step 1: noiseless recovery sanity check ---
    print("\n=== Step 1: noiseless recovery (sanity check on solve() itself) ===")
    max_noiseless_err = 0.0
    methods_seen = set()
    for dist_m in TEST_DISTANCES_M:
        for bearing_deg in TEST_BEARINGS_DEG:
            theta = np.radians(bearing_deg)
            true_xyz = DECK_ORIGIN + np.array([dist_m * np.cos(theta), dist_m * np.sin(theta), TEST_ALTITUDE_M])
            t_true = _true_arrival_us(nodes, true_xyz)
            try:
                result = solve(nodes, list(t_true))
            except ValueError as e:
                print(f"  dist={dist_m:4.0f}m bearing={bearing_deg:3.0f}: SOLVE FAILED — {e}")
                continue
            methods_seen.add(result.method)
            err = np.linalg.norm(np.array([result.x, result.y, result.z]) - true_xyz)
            max_noiseless_err = max(max_noiseless_err, err)
    print(f"  solver method(s) used: {sorted(methods_seen)}")
    print(f"  max position error across {len(TEST_DISTANCES_M) * len(TEST_BEARINGS_DEG)} noiseless trials: "
          f"{max_noiseless_err:.6f}m")
    if max_noiseless_err > 1e-3:
        print("  [FLAG] solve() does not exactly recover a noiseless synthetic source -- possible solver bug, "
              "investigate before trusting the noisy sweep below.")
    else:
        print("  solve() recovers noiseless synthetic sources essentially exactly -- no bug in the algebra itself.")

    # --- Step 2: noise sweep -- error magnitude AND inward-bias check ---
    print("\n=== Step 2: noise sweep (real node geometry, averaged across bearings) ===")
    print(f"{'dist_m':>7} {'noise_us':>9} {'median_err_m':>13} {'p90_err_m':>10} "
          f"{'mean_bias_toward_deck_m':>24} {'frac_pulled_inward':>19}")
    trials_per_bearing = max(1, args.trials // len(TEST_BEARINGS_DEG))
    for dist_m in TEST_DISTANCES_M:
        for sigma_us in NOISE_SIGMAS_US:
            errors, biases, n_pulled_inward, n_ok = [], [], 0, 0
            for bearing_deg in TEST_BEARINGS_DEG:
                theta = np.radians(bearing_deg)
                true_xyz = DECK_ORIGIN + np.array([dist_m * np.cos(theta), dist_m * np.sin(theta), TEST_ALTITUDE_M])
                true_dist_from_deck = float(np.linalg.norm(true_xyz - DECK_ORIGIN))
                t_true = _true_arrival_us(nodes, true_xyz)

                for _ in range(trials_per_bearing):
                    t_noisy = t_true + rng.normal(0.0, sigma_us, size=len(t_true))
                    try:
                        result = solve(nodes, list(t_noisy))
                    except ValueError:
                        continue
                    solved_xyz = np.array([result.x, result.y, result.z])
                    err = float(np.linalg.norm(solved_xyz - true_xyz))
                    solved_dist_from_deck = float(np.linalg.norm(solved_xyz - DECK_ORIGIN))
                    bias = solved_dist_from_deck - true_dist_from_deck  # negative = pulled toward deck
                    errors.append(err)
                    biases.append(bias)
                    if bias < 0:
                        n_pulled_inward += 1
                    n_ok += 1
            if n_ok == 0:
                print(f"{dist_m:7.0f} {sigma_us:9.0f}  ALL SOLVES FAILED")
                continue
            errors_arr, biases_arr = np.array(errors), np.array(biases)
            frac_inward = n_pulled_inward / n_ok
            print(f"{dist_m:7.0f} {sigma_us:9.0f} {np.median(errors_arr):13.2f} {np.percentile(errors_arr, 90):10.2f} "
                  f"{np.mean(biases_arr):24.2f} {frac_inward:19.1%}")

    print("\nInterpretation: Step 1 confirms whether solve() itself is algebraically correct on clean data --\n"
          "if max noiseless error is essentially zero, the four-unknowns-from-range-equations algebra is not the\n"
          "problem. In Step 2, 'mean_bias_toward_deck_m' meaningfully negative means solved positions land\n"
          "systematically CLOSER to the deck than the true source (a real inward pull, not just imprecision),\n"
          "and frac_pulled_inward well above 50% means most individual trials are pulled inward rather than\n"
          "scattered symmetrically. If bias stays small/symmetric (~50% frac_pulled_inward) even at noise levels\n"
          "matching real correlation-method disagreement (100us-several ms), the real array's observed collapse\n"
          "toward the deck is better explained by the TOA/correlation inputs being far noisier, more\n"
          "inconsistent, or non-Gaussian than this model -- not a solver-side bias.")


if __name__ == "__main__":
    main()
