"""
spotcheck_phat_masked_thresholds.py — checks the candidate phat_masked
trust-gate thresholds derived from the synthetic harness
(tools/calibrate_phat_masked_coef.py, 2026-07-31: MIN_PEAK_CORR_COEF~0.12-
0.16, AMBIGUOUS_RATIO_THRESHOLD~1.8-2.5) against real field audio, at the
lag/coefficient level only (NOT solved position -- see
project_soundhub_residual_self_consistency_degenerate/project_soundhub_
solver_inward_bias_confirmed for why position-level real-data checks are
currently uninformative at this array's ~10m aperture; the coefficient/
trust-gate level is unaffected by that problem).

Reuses the same real, DB-backed 6-attempt/24-node-pair batch as
validate_toa_real_pulls_v2.py (real per-attempt species bands/margins,
real per-node geometry) -- just re-applies several candidate threshold
pairs to the already-computed phat_masked scores instead of only
production's current (wrong-scale) 0.3/1.2.

Report-only: does not modify server/correlation.py, no DB writes.

Usage:
    python tools/spotcheck_phat_masked_thresholds.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import correlation as prod_correlation  # noqa: E402

from validate_toa_real_pulls import _load_node_positions  # noqa: E402
from validate_toa_real_pulls_v2 import (  # noqa: E402
    DEFAULT_AUDIO_DIR, DEFAULT_EXTRACT_DB, _analyze_real_attempt, _load_attempts,
)

# Candidates from the synthetic pooled calibration (keep ~80/90/95% of
# correct synthetic trials, see calibrate_phat_masked_coef.py's output).
CANDIDATES = [
    ("current production (0.3 / 1.2)", 0.30, 1.20),
    ("keep~95% synthetic (0.10 / 1.44)", 0.10, 1.44),
    ("keep~90% synthetic (0.12 / 1.80)", 0.12, 1.80),
    ("keep~80% synthetic (0.16 / 2.49)", 0.16, 2.49),
]


def main():
    positions = _load_node_positions(DEFAULT_EXTRACT_DB)
    attempts, nodes_by_attempt = _load_attempts(DEFAULT_EXTRACT_DB)

    pairs = []  # (attempt_id, node_id, coef, ratio)
    for attempt_id in sorted(attempts):
        attempt = attempts[attempt_id]
        result = _analyze_real_attempt(attempt, nodes_by_attempt.get(attempt_id, []), DEFAULT_AUDIO_DIR, positions)
        if result is None:
            continue
        for row in result["nodes"]:
            m = row["phat_masked"]
            pairs.append((attempt_id, row["node"], m["peak_corr_coef"], m["quality_ratio"]))

    print(f"\n{len(pairs)} real node-pair phat_masked scores (6 attempts x up to 4 neighbours each)\n")
    print(f"{'attempt':>8} {'node':>16} {'coef':>7} {'ratio':>7}")
    for attempt_id, node_id, coef, ratio in pairs:
        print(f"{attempt_id:8d} {node_id:>16s} {coef:7.3f} {ratio:7.3f}")

    print(f"\n{'candidate':38s} {'trusted':>10} {'pct':>6}")
    for label, coef_thresh, ratio_thresh in CANDIDATES:
        n_trusted = sum(1 for _, _, c, r in pairs if c >= coef_thresh and r >= ratio_thresh)
        pct = 100.0 * n_trusted / len(pairs) if pairs else 0.0
        print(f"{label:38s} {n_trusted:6d}/{len(pairs):<3d} {pct:5.1f}%")

    print("\nLag/coefficient-level spot check only -- does not evaluate solved position, "
          "which is currently uninformative at this array's aperture (see "
          "project_soundhub_solver_inward_bias_confirmed). Does not modify server/correlation.py.")


if __name__ == "__main__":
    main()
