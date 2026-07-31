"""
validate_toa_real_pulls.py — fast, no-ground-truth sanity check of
raw/gcc_phat/phat_masked against REAL field TDOA pull events in
test/tdoa_pulls/, before considering any production change to
server/correlation.py.

Why this exists (2026-07-29 discussion): production's default ('raw'/
'plain') is landing mostly 'untrusted' on real live attempts (see
project_soundhub_toa_investigation_pivot memory). Jon proposed trying
gcc_phat-style whitening directly on the hub rather than more offline
validation. Before touching production, this script gets real signal
cheaply: it measures the SAME peak_corr_coef/quality_ratio/trusted numbers
correlation.py itself gates on, against real messy field audio, with no
DB access and no production code changes.

Unlike validate_toa_methods.py (synthetic Xeno-canto, known injected
delay), there is no known true delay for a real field recording, so this
script cannot score lag error — only match confidence, plus each method's
implied lag side by side so results can be eyeballed for cross-method
agreement.

IMPORTANT coefficient-normalization note (fixed 2026-07-30): the first run
of this script showed gcc_phat's peak_corr_coef in the hundreds to
thousands on real attempts (should be roughly 0-1) — production's
peak_corr_coef formula is only mathematically bounded for 'plain'
correlation, not for a whitened/masked method like gcc_phat/phat_masked.
That would have made gcc_phat look confidently "trusted" almost
independent of match quality. Both methods here now use a properly-bounded
coefficient (see validate_toa_methods._max_possible_peak/_score_from_corr)
instead of production's own (buggy, for this branch) built-in one — this
means gcc_phat's reported coefficient in THIS script differs from what
correlation.py itself would compute if gcc_phat were ever actually wired
into production; that production-side bug is separate and unfixed.

IMPORTANT band-width note: with NO filtering applied at all, gcc_phat and
phat_masked are mathematically identical (both reduce to plain full-band
PHAT — see validate_toa_methods.py's _corr_phat_masked docstring for the
algebra). Running this fully broadband would make the comparison pointless
— there would be nothing for the reordering to fix. So this script applies
one FIXED, generic band (_GENERIC_BAND_LOW_HZ/_HIGH_HZ below, not
species-specific) to exercise the actual filter-then-whiten-vs-whiten-then-
mask distinction, while still avoiding the BirdNET/species-identification
work Jon asked to skip for this first pass.

Origin/neighbour pairing is reconstructed from filenames alone (confirmed
2026-07-29 against server/routes.py's WAV-naming code):
    audio_pull_<node>_<tStartUs>.wav        -- a server-requested pull
    audio_<node>_<tStartUs>_<mac>.wav       -- a self-triggered push
The origin is the self-triggered file with the EARLIEST t_start_us in the
attempt (some attempts have more than one self-triggered file — multiple
independent reporters of the same event — the earliest is the most likely
actual trigger). If an attempt has no self-triggered file at all (every
node independently reported, e.g. attempt_pied_16-21), the file with the
earliest t_start_us across all files is used instead and flagged as
approximate. There is no DB access here, so this is a best-effort
reconstruction, not a guaranteed match to whatever tdoa_attempts.
origin_node_id actually was historically for these specific attempts.

Report-only: does not modify server/correlation.py, does not touch the
live DB, does not touch config/species_tdoa_params.json.

Usage:
    python tools/validate_toa_real_pulls.py
    python tools/validate_toa_real_pulls.py --attempts-dir test/tdoa_pulls
    python tools/validate_toa_real_pulls.py --json-out real_pulls_report.json

Requires: numpy, scipy, soundfile. Run from the sound-hub root directory.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from math import gcd, sqrt

import numpy as np
from scipy.signal import resample_poly

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import correlation as prod_correlation  # noqa: E402
import onset_detection  # noqa: E402
from tdoa_solver import WORST_CASE_SPEED_OF_SOUND  # noqa: E402

from validate_toa_methods import _corr_phat_masked, _max_possible_peak, _score_from_corr  # noqa: E402

_PULL_RE = re.compile(r"^audio_pull_(?P<node>[A-Za-z0-9]+)_(?P<t_start_us>\d+)\.wav$", re.IGNORECASE)
_PUSH_RE = re.compile(r"^audio_(?P<node>[A-Za-z0-9]+)_(?P<t_start_us>\d+)_(?P<mac>[0-9a-fA-F]+)\.wav$", re.IGNORECASE)

# Live '__default__' onset threshold (config/species_tdoa_params.json's
# notes / project_soundhub_onset_threshold_p90 memory) — NOT
# onset_detection._ONSET_THRESHOLD_FACTOR (8.0), which is a last-resort
# Python-level fallback the live hub doesn't actually apply for an
# unconfigured species.
_DEFAULT_ONSET_THRESHOLD_FACTOR = 6.0

# See module docstring's "IMPORTANT band-width note" — a fixed, generic
# band (not derived from any species), just wide enough to plausibly cover
# most bird call content on this property, so there's an actual filter for
# gcc_phat to be broken by and for phat_masked to correctly survive.
_GENERIC_BAND_LOW_HZ = 500.0
_GENERIC_BAND_HIGH_HZ = 10000.0

METHODS = ("raw", "gcc_phat", "phat_masked")

DEFAULT_ATTEMPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "test", "tdoa_pulls")
DEFAULT_EXTRACT_DB = os.path.join(os.path.dirname(__file__), "..", "sound_hub_extract.db")


def _load_node_positions(db_path: str) -> dict:
    """Read node_positions (pos_e/pos_n/pos_alt) out of the small extract db
    (tools/extract_tdoa_rows.sql / fetch_hub_db.ps1) — real surveyed/
    estimated geometry for whatever nodes are currently in the live hub's
    node_positions table. 2026-07-30: the SAME extraction that was meant to
    pull tdoa_attempts/tdoa_attempt_nodes for these specific 3 historical
    attempts came back with ZERO rows for those two tables (their records
    have been pruned/rotated out of the live DB at some point) — but
    node_positions (always-current, not attempt-history) came through fine.
    So this script still can't recover the real origin_arrival_us/
    species_key for these specific old attempts, but it CAN now bound the
    correlation search to the physically real transit time between each
    node pair, instead of leaving it unbounded — a genuine partial upgrade
    even without the missing attempt history. Returns {} (all nodes
    unbounded, same as before) if db_path doesn't exist."""
    if not os.path.exists(db_path):
        return {}
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute("SELECT node_id, pos_e, pos_n, pos_alt FROM node_positions")
        return {row[0]: (row[1], row[2], row[3]) for row in cur.fetchall()}
    finally:
        con.close()


def _transit_s(positions: dict, node_a: str, node_b: str) -> float:
    """Real geometry-derived transit time bound between two nodes, using the
    same WORST_CASE_SPEED_OF_SOUND production itself uses for this exact
    purpose (routes.py's transit_s = dist_m / WORST_CASE_SPEED_OF_SOUND).
    Returns 0.0 (unbounded search, same fallback production uses for an
    unpositioned node) if either node has no position on file."""
    pos_a, pos_b = positions.get(node_a), positions.get(node_b)
    if pos_a is None or pos_b is None or None in pos_a or None in pos_b:
        return 0.0
    dist_m = sqrt(sum((a - b) ** 2 for a, b in zip(pos_a, pos_b)))
    return dist_m / WORST_CASE_SPEED_OF_SOUND


def _parse_filename(fname: str):
    """Return (node_id, t_start_us, is_push) or None if fname doesn't match
    either known production WAV-naming pattern."""
    m = _PULL_RE.match(fname)
    if m:
        return m.group("node"), int(m.group("t_start_us")), False
    m = _PUSH_RE.match(fname)
    if m:
        return m.group("node"), int(m.group("t_start_us")), True
    return None


def _pick_origin(files: list):
    """files: list of (path, node_id, t_start_us, is_push). Returns
    (origin_entry, approximate) — see module docstring for the rule."""
    pushes = [f for f in files if f[3]]
    if pushes:
        return min(pushes, key=lambda f: f[2]), len(pushes) > 1
    return min(files, key=lambda f: f[2]), True


def _trusted(score: dict) -> bool:
    return (
        score["peak_corr_coef"] >= prod_correlation.MIN_PEAK_CORR_COEF
        and score["quality_ratio"] >= prod_correlation.AMBIGUOUS_RATIO_THRESHOLD
    )


def _corr_gcc_phat_local(a: np.ndarray, b: np.ndarray):
    """Mirrors correlation.py's gcc_phat branch exactly (same FFT/normalize/
    IFFT math) — duplicated locally, rather than calling
    prod_correlation._score_correlation(..., "gcc_phat") directly, because
    that function only returns the FINAL dict, not R(f) itself, and R is
    needed to compute a properly-bounded peak_corr_coef (see
    validate_toa_methods._score_from_corr's docstring for the full story on
    why correlation.py's own built-in coefficient is wrong for this branch).
    Returns (corr, max_possible_peak) in the same layout/convention as
    validate_toa_methods._corr_scot/_corr_phat_masked, so it can be scored
    with the same _score_from_corr helper.

    IMPORTANT: this fixes the coefficient in THIS SCRIPT's report only.
    correlation.py's own internal peak_corr_coef computation for its
    gcc_phat branch still has the bug described above — that's a separate,
    production-code fix that would need to happen (and be walked through
    explicitly, per this project's confirm-before-editing convention)
    before gcc_phat/phat_masked could ever be safely wired into the live
    hub's trust gate."""
    n = len(a) + len(b) - 1
    n_fft = 1
    while n_fft < n:
        n_fft *= 2
    A = np.fft.rfft(a, n_fft)
    B = np.fft.rfft(b, n_fft)
    R = A * np.conj(B)
    denom = np.abs(R)
    denom[denom == 0] = 1e-12
    R_white = R / denom
    corr_full = np.fft.irfft(R_white, n_fft)
    corr = np.concatenate((corr_full[-(len(b) - 1):], corr_full[: len(a)]))
    return corr, _max_possible_peak(R_white, n_fft)


def _analyze_attempt(attempt_dir: str, threshold_factor: float, positions: dict) -> dict | None:
    fnames = sorted(f for f in os.listdir(attempt_dir) if f.lower().endswith(".wav"))
    files = []
    for fname in fnames:
        parsed = _parse_filename(fname)
        if parsed is None:
            print(f"    [warn] unrecognized filename, skipping: {fname}")
            continue
        node_id, t_start_us, is_push = parsed
        files.append((os.path.join(attempt_dir, fname), node_id, t_start_us, is_push))

    if len(files) < 2:
        print("  [FLAG] fewer than 2 usable files, skipping")
        return None

    origin, approximate = _pick_origin(files)
    origin_path, origin_node, origin_t_start_us, _ = origin
    flag = " (APPROXIMATE -- multiple/no self-triggered candidates)" if approximate else ""
    print(f"  origin: {origin_node} ({os.path.basename(origin_path)}){flag}")

    try:
        origin_arrival_us, onset_ratio = onset_detection.detect_onset_us(
            "global_peak", origin_path, origin_t_start_us,
            threshold_factor=threshold_factor,
            freq_band_low_hz=_GENERIC_BAND_LOW_HZ, freq_band_high_hz=_GENERIC_BAND_HIGH_HZ,
        )
    except onset_detection.OnsetNotFoundError as e:
        print(f"  [FLAG] no onset found on origin file: {e}")
        return None
    print(f"  origin onset: arrival_us={origin_arrival_us:.0f} (ratio={onset_ratio:.2f})")

    origin_rate, origin_data = onset_detection._load_mono(origin_path)
    origin_data = onset_detection.bandpass_filter(origin_data, origin_rate, _GENERIC_BAND_LOW_HZ, _GENERIC_BAND_HIGH_HZ)
    # Raw (unfiltered) copy kept too, purely for phat_masked -- see
    # validate_toa_methods.py's _corr_phat_masked docstring for why it must
    # whiten the UNFILTERED signal, not this already-filtered one.
    origin_raw_rate, origin_raw_data = onset_detection._load_mono(origin_path)

    node_rows = []
    for path, node_id, t_start_us, is_push in files:
        if path == origin_path:
            continue
        neighbor_rate, neighbor_filt = onset_detection._load_mono(path)
        neighbor_raw_rate, neighbor_raw = onset_detection._load_mono(path)
        neighbor_filt = onset_detection.bandpass_filter(
            neighbor_filt, neighbor_rate, _GENERIC_BAND_LOW_HZ, _GENERIC_BAND_HIGH_HZ)

        if origin_rate != neighbor_rate:
            g = gcd(origin_rate, neighbor_rate)
            neighbor_filt = resample_poly(neighbor_filt, origin_rate // g, neighbor_rate // g)
            neighbor_raw = resample_poly(neighbor_raw, origin_rate // g, neighbor_rate // g)
            neighbor_rate = origin_rate
        rate = origin_rate

        origin_center = int(round((origin_arrival_us - origin_t_start_us) * 1e-6 * rate))
        neighbor_center = int(round((origin_arrival_us - t_start_us) * 1e-6 * rate))

        # Real geometry-derived transit bound (2026-07-30) -- 0.0 (unbounded,
        # same as before) if either node has no surveyed/estimated position
        # in the extract db. Widens the neighbour's search window by this
        # amount on each side, same convention correlate_leading_edge itself
        # uses, so the true alignment (which may sit up to transit_s away
        # from the naive zero-geometry center) is findable at all, while also
        # bounding which peak can be SELECTED to only physically-possible
        # lags -- not just "unambiguous", but "couldn't have arrived any
        # other way given the real distance between these two nodes".
        transit_s = _transit_s(positions, origin_node, node_id)
        transit_ms = transit_s * 1e3

        a_filt = prod_correlation._trim_leading_edge(origin_data, rate, origin_center)
        b_filt = prod_correlation._trim_leading_edge(
            neighbor_filt, rate, neighbor_center,
            pre_ms=prod_correlation.LEADING_EDGE_PRE_MS + transit_ms,
            post_ms=prod_correlation.LEADING_EDGE_POST_MS + transit_ms,
        )
        a_raw = prod_correlation._trim_leading_edge(origin_raw_data, rate, origin_center)
        b_raw = prod_correlation._trim_leading_edge(
            neighbor_raw, rate, neighbor_center,
            pre_ms=prod_correlation.LEADING_EDGE_PRE_MS + transit_ms,
            post_ms=prod_correlation.LEADING_EDGE_POST_MS + transit_ms,
        )
        if len(a_filt) < prod_correlation._MIN_TRIM_SAMPLES or len(b_filt) < prod_correlation._MIN_TRIM_SAMPLES:
            print(f"    {node_id:16s} [FLAG] not enough clean room to correlate, skipping")
            continue

        # Widening b's PRE side by transit_ms shifts b's own local-index-0
        # earlier than a's by that amount, independent of any real delay --
        # must be subtracted back out of every method's raw lag_us, same
        # correction correlate_leading_edge itself applies.
        transit_us_offset = transit_s * 1e6

        row = {"node": node_id, "is_push": is_push, "transit_s": transit_s}

        # 'raw' uses production's own _score_correlation/peak_corr_coef
        # unchanged -- its Cauchy-Schwarz-bounded coefficient is valid for
        # 'plain' (raw-amplitude) correlation, so no fix is needed here.
        raw_score = prod_correlation._score_correlation(rate, a_filt, b_filt, "plain", transit_s=transit_s)
        raw_score["lag_us"] -= transit_us_offset
        row["raw"] = {**raw_score, "trusted": _trusted(raw_score)}

        # Today's actual production ordering: bandpass FIRST (a_filt/b_filt),
        # THEN PHAT-whiten -- the real bug, not an idealized version of it.
        # Uses the LOCAL gcc_phat scorer (not prod_correlation._score_
        # correlation's built-in one) so peak_corr_coef is properly bounded
        # -- see _corr_gcc_phat_local's docstring for why.
        phat_corr, phat_ceiling = _corr_gcc_phat_local(a_filt, b_filt)
        phat_score = _score_from_corr(
            phat_corr, rate, a_filt, b_filt, max_possible_peak=phat_ceiling, transit_s=transit_s)
        phat_score["lag_us"] -= transit_us_offset
        row["gcc_phat"] = {**phat_score, "trusted": _trusted(phat_score)}

        # Jon's proposed fix: whiten the RAW, unfiltered signal, THEN mask.
        masked_corr, masked_ceiling = _corr_phat_masked(
            a_raw, b_raw, rate, _GENERIC_BAND_LOW_HZ, _GENERIC_BAND_HIGH_HZ)
        masked_score = _score_from_corr(
            masked_corr, rate, a_raw, b_raw, max_possible_peak=masked_ceiling, transit_s=transit_s)
        masked_score["lag_us"] -= transit_us_offset
        row["phat_masked"] = {**masked_score, "trusted": _trusted(masked_score)}

        node_rows.append(row)
        geom_note = f" transit={transit_s*1e3:.1f}ms" if transit_s > 0 else " transit=UNBOUNDED (no position)"
        print(f"    {node_id:16s} ({'push' if is_push else 'pull'}){geom_note}")
        for m in METHODS:
            r = row[m]
            ratio_str = f"{min(r['quality_ratio'], 999.0):7.2f}" if np.isfinite(r["quality_ratio"]) else "    inf"
            print(f"      {m:12s}: lag={r['lag_us']:+9.1f}us  coef={r['peak_corr_coef']:.3f}  "
                  f"ratio={ratio_str}  trusted={r['trusted']}")

    return {
        "origin_node": origin_node, "origin_approximate": approximate,
        "origin_arrival_us": origin_arrival_us, "onset_ratio": onset_ratio,
        "nodes": node_rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--attempts-dir", default=DEFAULT_ATTEMPTS_DIR)
    parser.add_argument("--extract-db", default=DEFAULT_EXTRACT_DB,
                         help="sqlite db with a node_positions table (tools/fetch_hub_db.ps1's output). "
                              "Missing/absent nodes fall back to unbounded search, same as before.")
    parser.add_argument("--threshold-factor", type=float, default=_DEFAULT_ONSET_THRESHOLD_FACTOR)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    positions = _load_node_positions(args.extract_db)
    if positions:
        print(f"Loaded {len(positions)} node position(s) from {args.extract_db}")
    else:
        print(f"[warn] no node_positions found at {args.extract_db} -- all searches will be unbounded")

    attempt_names = sorted(
        d for d in os.listdir(args.attempts_dir)
        if os.path.isdir(os.path.join(args.attempts_dir, d))
    )

    full_report = {}
    trusted_counts = {m: {"trusted": 0, "total": 0} for m in METHODS}
    lag_agreement = {m: [] for m in METHODS if m != "raw"}

    for name in attempt_names:
        print(f"\n=== {name} ===")
        result = _analyze_attempt(os.path.join(args.attempts_dir, name), args.threshold_factor, positions)
        if result is None:
            continue
        full_report[name] = result
        for row in result["nodes"]:
            for m in METHODS:
                trusted_counts[m]["total"] += 1
                if row[m]["trusted"]:
                    trusted_counts[m]["trusted"] += 1
            for m in lag_agreement:
                lag_agreement[m].append(abs(row[m]["lag_us"] - row["raw"]["lag_us"]))

    print("\n=== Trusted rate across all real node pairs ===")
    for m in METHODS:
        c = trusted_counts[m]
        pct = (100.0 * c["trusted"] / c["total"]) if c["total"] else 0.0
        print(f"  {m:12s}: {c['trusted']}/{c['total']} trusted ({pct:.1f}%)")

    print("\n=== Lag agreement with raw (|method - raw|, real pairs only) ===")
    for m, diffs in lag_agreement.items():
        if not diffs:
            print(f"  {m:12s}: no data")
            continue
        arr = np.array(diffs)
        print(f"  {m:12s}: median={np.median(arr):9.1f}us  mean={np.mean(arr):9.1f}us  max={np.max(arr):9.1f}us")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(full_report, f, indent=2, default=str)
        print(f"\nWrote full report to {args.json_out}")

    print(f"\nReal-field sanity check only -- no ground truth available, this measures match "
          f"confidence (peak_corr_coef/quality_ratio/trusted) not lag accuracy. Fixed generic "
          f"band [{_GENERIC_BAND_LOW_HZ:.0f},{_GENERIC_BAND_HIGH_HZ:.0f}]Hz, not species-specific "
          f"-- see module docstring.")


if __name__ == "__main__":
    main()
