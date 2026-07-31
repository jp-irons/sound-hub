"""
validate_toa_real_pulls_v2.py — DB-backed comparison of raw/gcc_phat/
phat_masked against REAL production tdoa_attempts (2026-07-30 batch: 6 real,
'solved' attempts spanning 4 species — Rainbow Lorikeet, Gray Butcherbird x2,
Olive-backed Oriole, Noisy Miner x2), using the actual per-attempt
snapshotted species band/margins, the actual per-node arrival_us production
computed, and real node_positions geometry — all pulled from the live hub
via tools/fetch_hub_db.ps1-style extraction (see the 2026-07-30 discussion
for the full chain: sqlite3 extraction -> filename matching against
tdoa_attempt_nodes -> tar of exactly the needed WAVs).

Why this is meaningfully stronger than validate_toa_real_pulls.py (the
earlier, self-contained version): that script had to re-derive
origin_arrival_us via its own onset detection (no DB access), guess which
file was the origin, and use one fixed generic band since it didn't know
species per attempt. All three of those are now real: tdoa_attempts.
freq_band_low_hz/high_hz/window_margin_pre_ms/post_ms are the actual
snapshotted values production used for this specific attempt (not a
generic guess), tdoa_attempts.origin_node_id is the real origin (not a
heuristic), and tdoa_attempt_nodes.arrival_us gives production's own real
computed arrival time for EVERY node, including the origin AND every
neighbour.

IMPORTANT — what this still is and isn't: tdoa_attempt_nodes.arrival_us is
NOT independent ground truth. Every one of these attempts used
correlation_method='plain' (confirmed in the extract — see the printed
per-attempt config), so a neighbour's stored arrival_us is itself that
node's real 'plain'-correlation output, already vetted enough to have
status='arrived' live. This script therefore measures "how much would each
candidate method's estimate differ from what production's own 'plain'
method already computed and trusted for this exact real event" — a
delta-from-production metric — not "which method is objectively correct".
That said, it's a very informative delta: 'raw' (which uses the same
'plain' method against the same real audio, same real window sizing)
should land very close to zero delta as an internal consistency check
before trusting gcc_phat/phat_masked's deltas at all. A candidate method
landing consistently far from production's trusted value, especially with
a low confidence score, is a real signal it would have changed the outcome
on events production already got right.

Report-only: does not modify server/correlation.py, does not write to the
live DB, does not touch config/species_tdoa_params.json.

Usage:
    python tools/validate_toa_real_pulls_v2.py
    python tools/validate_toa_real_pulls_v2.py --json-out real_pulls_v2_report.json

Requires: numpy, scipy, soundfile. Run from the sound-hub root directory.
"""

import argparse
import json
import os
import sqlite3
import sys
from math import gcd

import numpy as np
from scipy.signal import resample_poly

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import correlation as prod_correlation  # noqa: E402
import onset_detection  # noqa: E402

from validate_toa_methods import _corr_phat_masked, _max_possible_peak, _score_from_corr  # noqa: E402
from validate_toa_real_pulls import (  # noqa: E402
    _corr_gcc_phat_local, _load_node_positions, _parse_filename, _transit_s, _trusted,
)

METHODS = ("raw", "gcc_phat", "phat_masked")

DEFAULT_AUDIO_DIR = os.path.join(os.path.dirname(__file__), "..", "test", "tdoa_real_batch")
DEFAULT_EXTRACT_DB = os.path.join(os.path.dirname(__file__), "..", "sound_hub_extract2.db")


def _load_attempts(db_path: str) -> tuple:
    """Returns (attempts: {id: row_dict}, nodes_by_attempt: {id: [row_dict]})."""
    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        attempts = {
            row["id"]: dict(row)
            for row in con.execute("SELECT * FROM tdoa_attempts")
        }
        nodes_by_attempt = {}
        for row in con.execute("SELECT * FROM tdoa_attempt_nodes ORDER BY attempt_id, node_id"):
            nodes_by_attempt.setdefault(row["attempt_id"], []).append(dict(row))
        return attempts, nodes_by_attempt
    finally:
        con.close()


def _match_file(audio_dir: str, node_id: str, t_ref_us: int, prefer_push: bool,
                 window_us: int = 15_000_000):
    """Same closest-match-by-embedded-timestamp logic as the hub-side
    extraction script that built this batch, reapplied locally so the
    (attempt, node) -> filename mapping is derived from data, not
    transcribed by hand from the printed table."""
    candidates = []
    for fname in os.listdir(audio_dir):
        parsed = _parse_filename(fname)
        if parsed is None:
            continue
        f_node, f_t, is_push = parsed
        if f_node != node_id or abs(f_t - t_ref_us) > window_us:
            continue
        candidates.append((f_t, is_push, fname))
    if not candidates:
        return None
    if prefer_push:
        pushes = [c for c in candidates if c[1]]
        pool = pushes if pushes else candidates
    else:
        pool = candidates
    return min(pool, key=lambda c: abs(c[0] - t_ref_us))[2]


def _analyze_real_attempt(attempt: dict, node_rows: list, audio_dir: str, positions: dict) -> dict | None:
    attempt_id = attempt["id"]
    species = attempt["species_key"]
    origin_node = attempt["origin_node_id"]
    band_lo, band_hi = attempt["freq_band_low_hz"], attempt["freq_band_high_hz"]
    pre_ms = attempt["window_margin_pre_ms"] or prod_correlation.LEADING_EDGE_PRE_MS
    post_ms = attempt["window_margin_post_ms"] or prod_correlation.LEADING_EDGE_POST_MS

    node_info = {r["node_id"]: r for r in node_rows}
    if origin_node not in node_info or node_info[origin_node]["arrival_us"] is None:
        print(f"  [FLAG] no origin arrival_us for attempt {attempt_id}, skipping")
        return None
    origin_arrival_us = node_info[origin_node]["arrival_us"]

    origin_fname = _match_file(audio_dir, origin_node, attempt["t_start_us"], prefer_push=True)
    if origin_fname is None:
        print(f"  [FLAG] no origin audio file found for attempt {attempt_id}, skipping")
        return None
    origin_path = os.path.join(audio_dir, origin_fname)
    origin_parsed = _parse_filename(origin_fname)
    origin_t_start_us = origin_parsed[1]

    print(f"  species={species} band=[{band_lo},{band_hi}]Hz margins=[{pre_ms},{post_ms}]ms "
          f"origin={origin_node} ({origin_fname})")

    origin_rate, origin_raw_data = onset_detection._load_mono(origin_path)
    if band_lo is not None and band_hi is not None:
        origin_filt = onset_detection.bandpass_filter(origin_raw_data, origin_rate, band_lo, band_hi)
    else:
        origin_filt = origin_raw_data

    node_rows_out = []
    for node_id, row in sorted(node_info.items()):
        if node_id == origin_node:
            continue
        real_arrival_us = row["arrival_us"]
        if real_arrival_us is None:
            print(f"    {node_id:16s} [FLAG] no real arrival_us on file, skipping")
            continue

        neighbor_fname = _match_file(audio_dir, node_id, attempt["t_start_us"], prefer_push=False)
        if neighbor_fname is None:
            print(f"    {node_id:16s} [FLAG] no audio file found, skipping")
            continue
        neighbor_path = os.path.join(audio_dir, neighbor_fname)
        neighbor_t_start_us = _parse_filename(neighbor_fname)[1]

        neighbor_rate, neighbor_raw = onset_detection._load_mono(neighbor_path)
        if band_lo is not None and band_hi is not None:
            neighbor_filt = onset_detection.bandpass_filter(neighbor_raw, neighbor_rate, band_lo, band_hi)
        else:
            neighbor_filt = neighbor_raw

        if origin_rate != neighbor_rate:
            g = gcd(origin_rate, neighbor_rate)
            neighbor_filt = resample_poly(neighbor_filt, origin_rate // g, neighbor_rate // g)
            neighbor_raw = resample_poly(neighbor_raw, origin_rate // g, neighbor_rate // g)
            neighbor_rate = origin_rate
        rate = origin_rate

        transit_s = _transit_s(positions, origin_node, node_id)
        transit_ms = transit_s * 1e3

        origin_center = int(round((origin_arrival_us - origin_t_start_us) * 1e-6 * rate))
        neighbor_center = int(round((origin_arrival_us - neighbor_t_start_us) * 1e-6 * rate))

        a_filt = prod_correlation._trim_leading_edge(origin_filt, rate, origin_center, pre_ms=pre_ms, post_ms=post_ms)
        b_filt = prod_correlation._trim_leading_edge(
            neighbor_filt, rate, neighbor_center, pre_ms=pre_ms + transit_ms, post_ms=post_ms + transit_ms)
        a_raw = prod_correlation._trim_leading_edge(origin_raw_data, rate, origin_center, pre_ms=pre_ms, post_ms=post_ms)
        b_raw = prod_correlation._trim_leading_edge(
            neighbor_raw, rate, neighbor_center, pre_ms=pre_ms + transit_ms, post_ms=post_ms + transit_ms)
        if len(a_filt) < prod_correlation._MIN_TRIM_SAMPLES or len(b_filt) < prod_correlation._MIN_TRIM_SAMPLES:
            print(f"    {node_id:16s} [FLAG] not enough clean room to correlate, skipping")
            continue

        transit_us_offset = transit_s * 1e6
        row_out = {"node": node_id, "real_arrival_us": real_arrival_us, "transit_s": transit_s}

        raw_score = prod_correlation._score_correlation(rate, a_filt, b_filt, "plain", transit_s=transit_s)
        raw_score["lag_us"] -= transit_us_offset
        row_out["raw"] = {**raw_score, "trusted": _trusted(raw_score)}

        phat_corr, phat_ceiling = _corr_gcc_phat_local(a_filt, b_filt)
        phat_score = _score_from_corr(phat_corr, rate, a_filt, b_filt, max_possible_peak=phat_ceiling, transit_s=transit_s)
        phat_score["lag_us"] -= transit_us_offset
        row_out["gcc_phat"] = {**phat_score, "trusted": _trusted(phat_score)}

        masked_corr, masked_ceiling = _corr_phat_masked(a_raw, b_raw, rate, band_lo, band_hi)
        masked_score = _score_from_corr(masked_corr, rate, a_raw, b_raw, max_possible_peak=masked_ceiling, transit_s=transit_s)
        masked_score["lag_us"] -= transit_us_offset
        row_out["phat_masked"] = {**masked_score, "trusted": _trusted(masked_score)}

        node_rows_out.append(row_out)
        print(f"    {node_id:16s} real_arrival_us={real_arrival_us:.0f} transit={transit_s*1e3:.1f}ms")
        for m in METHODS:
            r = row_out[m]
            est_arrival_us = origin_arrival_us + r["lag_us"]
            delta_us = est_arrival_us - real_arrival_us
            ratio_str = f"{min(r['quality_ratio'], 999.0):7.2f}" if np.isfinite(r["quality_ratio"]) else "    inf"
            print(f"      {m:12s}: delta_from_real={delta_us:+9.1f}us  coef={r['peak_corr_coef']:.3f}  "
                  f"ratio={ratio_str}  trusted={r['trusted']}")
            r["delta_from_real_us"] = delta_us

    return {
        "attempt_id": attempt_id, "species": species, "origin_node": origin_node,
        "origin_arrival_us": origin_arrival_us, "nodes": node_rows_out,
    }


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
    trusted_counts = {m: {"trusted": 0, "total": 0} for m in METHODS}
    delta_stats = {m: [] for m in METHODS}

    for attempt_id in sorted(attempts):
        print(f"\n=== attempt {attempt_id} ===")
        result = _analyze_real_attempt(
            attempts[attempt_id], nodes_by_attempt.get(attempt_id, []), args.audio_dir, positions)
        if result is None:
            continue
        full_report[attempt_id] = result
        for row in result["nodes"]:
            for m in METHODS:
                trusted_counts[m]["total"] += 1
                if row[m]["trusted"]:
                    trusted_counts[m]["trusted"] += 1
                delta_stats[m].append(row[m]["delta_from_real_us"])

    print("\n=== Trusted rate across all real node pairs ===")
    for m in METHODS:
        c = trusted_counts[m]
        pct = (100.0 * c["trusted"] / c["total"]) if c["total"] else 0.0
        print(f"  {m:12s}: {c['trusted']}/{c['total']} trusted ({pct:.1f}%)")

    print("\n=== Delta from production's own real arrival_us (signed, us) ===")
    for m in METHODS:
        d = np.array(delta_stats[m])
        if len(d) == 0:
            print(f"  {m:12s}: no data")
            continue
        print(f"  {m:12s}: bias={np.mean(d):+9.1f}us  mean|delta|={np.mean(np.abs(d)):9.1f}us  "
              f"median|delta|={np.median(np.abs(d)):9.1f}us  max|delta|={np.max(np.abs(d)):9.1f}us")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(full_report, f, indent=2, default=str)
        print(f"\nWrote full report to {args.json_out}")

    print("\nDB-backed real-attempt comparison -- delta is relative to production's own "
          "real (trusted, 'plain'-method) arrival_us, not independent ground truth. See "
          "module docstring for why 'raw' landing near zero delta is the key sanity check "
          "before trusting gcc_phat/phat_masked's deltas.")


if __name__ == "__main__":
    main()
