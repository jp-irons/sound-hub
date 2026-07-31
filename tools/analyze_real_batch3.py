"""
analyze_real_batch3.py — computes raw/phat_masked peak_corr_coef/
quality_ratio across the large, mixed-status real batch pulled 2026-07-31
(sound_hub_extract3.db: 693 attempts/574 solved+119 failed, ~24h window,
11 species, real audio_event_id->filename join -- no heuristic file
matching needed, see test/tdoa_real_batch3/).

Unlike the original 6-attempt/24-pair batch (validate_toa_real_pulls_v2.py),
this one includes production's genuinely UNTRUSTED real correlations
(correlation_status='untrusted', 2054 of ~2551 node rows that reached
correlation) alongside trusted ones (493) -- the actual population the
original investigation was about, not just cases production already
succeeded on.

Per attempt: uses the real per-attempt species_key/freq_band_low_hz/high_hz/
window_margin_pre_ms/post_ms from tdoa_attempts (same convention as v2), the
real origin_node_id, and looks up each node's real WAV directly via
tdoa_attempt_nodes.audio_event_id -> audio_events.filename (exact, no
timestamp-proximity guessing). Origin's own arrival_us (from its
tdoa_attempt_nodes row) is used as the reference onset time, same as v2.

Chunked by attempt ID range (--start-idx/--limit) so a full 693-attempt
pass can run across several calls within the sandbox's per-call time
budget -- writes one row per real node pair to --csv-out (append mode).

Report-only: does not modify server/correlation.py, no DB writes.

Usage:
    python tools/analyze_real_batch3.py --start-idx 0 --limit 50 --csv-out /tmp/batch3_scores.csv
"""

import argparse
import csv
import os
import sqlite3
import sys
from math import gcd

import numpy as np
from scipy.signal import resample_poly

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import correlation as prod_correlation  # noqa: E402
import onset_detection  # noqa: E402
from tdoa_solver import WORST_CASE_SPEED_OF_SOUND  # noqa: E402

from validate_toa_methods import _corr_phat_masked, _max_possible_peak, _score_from_corr  # noqa: E402

DEFAULT_AUDIO_DIR = os.path.join(os.path.dirname(__file__), "..", "test", "tdoa_real_batch3")
DEFAULT_EXTRACT_DB = os.path.join(os.path.dirname(__file__), "..", "sound_hub_extract3.db")

METHODS = ("raw", "phat_masked")


def _corr_gcc_phat_local(a, b):
    # unused here (kept out to save time) -- present for parity if needed later
    pass


def _load_everything(db_path: str):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    attempts = {row["id"]: dict(row) for row in con.execute(
        "SELECT * FROM tdoa_attempts ORDER BY id")}
    nodes_by_attempt = {}
    for row in con.execute("SELECT * FROM tdoa_attempt_nodes ORDER BY attempt_id, node_id"):
        nodes_by_attempt.setdefault(row["attempt_id"], []).append(dict(row))
    audio_events = {row["id"]: dict(row) for row in con.execute(
        "SELECT * FROM audio_events WHERE filename IS NOT NULL")}
    positions = {row["node_id"]: (row["pos_e"], row["pos_n"], row["pos_alt"])
                 for row in con.execute("SELECT * FROM node_positions")}
    con.close()
    return attempts, nodes_by_attempt, audio_events, positions


def _transit_s(positions, node_a, node_b):
    pa, pb = positions.get(node_a), positions.get(node_b)
    if pa is None or pb is None or None in pa or None in pb:
        return 0.0
    dist = float(np.sqrt(sum((x - y) ** 2 for x, y in zip(pa, pb))))
    return dist / WORST_CASE_SPEED_OF_SOUND


def _score_pair(origin_raw, origin_rate, origin_arrival_us, origin_t_start_us,
                 neighbor_raw, neighbor_rate, neighbor_t_start_us,
                 band_lo, band_hi, pre_ms, post_ms, transit_s):
    if neighbor_rate != origin_rate:
        g = gcd(origin_rate, neighbor_rate)
        neighbor_raw = resample_poly(neighbor_raw, origin_rate // g, neighbor_rate // g)
        neighbor_rate = origin_rate
    rate = origin_rate
    transit_ms = transit_s * 1e3

    if band_lo is not None and band_hi is not None:
        origin_filt = onset_detection.bandpass_filter(origin_raw, rate, band_lo, band_hi)
        neighbor_filt = onset_detection.bandpass_filter(neighbor_raw, rate, band_lo, band_hi)
    else:
        origin_filt, neighbor_filt = origin_raw, neighbor_raw

    origin_center = int(round((origin_arrival_us - origin_t_start_us) * 1e-6 * rate))
    neighbor_center = int(round((origin_arrival_us - neighbor_t_start_us) * 1e-6 * rate))

    a_filt = prod_correlation._trim_leading_edge(origin_filt, rate, origin_center, pre_ms=pre_ms, post_ms=post_ms)
    b_filt = prod_correlation._trim_leading_edge(
        neighbor_filt, rate, neighbor_center, pre_ms=pre_ms + transit_ms, post_ms=post_ms + transit_ms)
    a_raw = prod_correlation._trim_leading_edge(origin_raw, rate, origin_center, pre_ms=pre_ms, post_ms=post_ms)
    b_raw = prod_correlation._trim_leading_edge(
        neighbor_raw, rate, neighbor_center, pre_ms=pre_ms + transit_ms, post_ms=post_ms + transit_ms)
    if len(a_filt) < prod_correlation._MIN_TRIM_SAMPLES or len(b_filt) < prod_correlation._MIN_TRIM_SAMPLES:
        return None

    raw_score = prod_correlation._score_correlation(rate, a_filt, b_filt, "plain", transit_s=transit_s)
    masked_corr, masked_ceiling = _corr_phat_masked(a_raw, b_raw, rate, band_lo, band_hi)
    masked_score = _score_from_corr(masked_corr, rate, a_raw, b_raw, max_possible_peak=masked_ceiling, transit_s=transit_s)

    return {"raw": raw_score, "phat_masked": masked_score}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audio-dir", default=DEFAULT_AUDIO_DIR)
    parser.add_argument("--extract-db", default=DEFAULT_EXTRACT_DB)
    parser.add_argument("--start-idx", type=int, default=0, help="index into sorted attempt-id list to start at")
    parser.add_argument("--limit", type=int, default=50, help="number of attempts to process this run")
    parser.add_argument("--csv-out", required=True)
    args = parser.parse_args()

    attempts, nodes_by_attempt, audio_events, positions = _load_everything(args.extract_db)
    attempt_ids = sorted(attempts)
    chunk = attempt_ids[args.start_idx: args.start_idx + args.limit]
    print(f"Total attempts in DB: {len(attempt_ids)}. Processing idx [{args.start_idx}:{args.start_idx + args.limit}) -> {len(chunk)} attempt(s).")

    write_header = not os.path.exists(args.csv_out)
    n_rows = 0
    n_skipped_no_audio = 0
    n_skipped_no_origin = 0
    n_skipped_too_short = 0

    with open(args.csv_out, "a", newline="") as fh:
        writer = csv.writer(fh)
        if write_header:
            writer.writerow(["attempt_id", "species", "attempt_status", "origin_node", "neighbor_node",
                              "node_status", "correlation_status", "real_arrival_us",
                              "raw_coef", "raw_ratio", "raw_lag_us",
                              "masked_coef", "masked_ratio", "masked_lag_us"])

        for attempt_id in chunk:
            attempt = attempts[attempt_id]
            origin_node = attempt["origin_node_id"]
            band_lo, band_hi = attempt["freq_band_low_hz"], attempt["freq_band_high_hz"]
            pre_ms = attempt["window_margin_pre_ms"] or prod_correlation.LEADING_EDGE_PRE_MS
            post_ms = attempt["window_margin_post_ms"] or prod_correlation.LEADING_EDGE_POST_MS

            node_rows = nodes_by_attempt.get(attempt_id, [])
            node_info = {r["node_id"]: r for r in node_rows}
            origin_row = node_info.get(origin_node)
            if origin_row is None or origin_row.get("audio_event_id") not in audio_events:
                n_skipped_no_origin += 1
                continue
            origin_ae = audio_events[origin_row["audio_event_id"]]
            origin_arrival_us = origin_row.get("arrival_us")
            if origin_arrival_us is None:
                n_skipped_no_origin += 1
                continue
            origin_path = os.path.join(args.audio_dir, origin_ae["filename"])
            if not os.path.exists(origin_path):
                n_skipped_no_audio += 1
                continue
            try:
                origin_rate, origin_raw = onset_detection._load_mono(origin_path)
            except Exception:
                n_skipped_no_audio += 1
                continue
            origin_t_start_us = origin_ae["t_start_us"]

            for node_id, row in sorted(node_info.items()):
                if node_id == origin_node:
                    continue
                ae_id = row.get("audio_event_id")
                if ae_id not in audio_events:
                    continue
                ae = audio_events[ae_id]
                neighbor_path = os.path.join(args.audio_dir, ae["filename"])
                if not os.path.exists(neighbor_path):
                    n_skipped_no_audio += 1
                    continue
                try:
                    neighbor_rate, neighbor_raw = onset_detection._load_mono(neighbor_path)
                except Exception:
                    n_skipped_no_audio += 1
                    continue
                neighbor_t_start_us = ae["t_start_us"]

                transit_s = _transit_s(positions, origin_node, node_id)
                scores = _score_pair(
                    origin_raw, origin_rate, origin_arrival_us, origin_t_start_us,
                    neighbor_raw, neighbor_rate, neighbor_t_start_us,
                    band_lo, band_hi, pre_ms, post_ms, transit_s,
                )
                if scores is None:
                    n_skipped_too_short += 1
                    continue

                writer.writerow([
                    attempt_id, attempt["species_key"], attempt["status"], origin_node, node_id,
                    row["status"], row.get("correlation_status"), row.get("arrival_us"),
                    scores["raw"]["peak_corr_coef"], scores["raw"]["quality_ratio"], scores["raw"]["lag_us"],
                    scores["phat_masked"]["peak_corr_coef"], scores["phat_masked"]["quality_ratio"], scores["phat_masked"]["lag_us"],
                ])
                n_rows += 1

    print(f"Wrote {n_rows} row(s) to {args.csv_out}. "
          f"Skipped: no_origin={n_skipped_no_origin} no_audio={n_skipped_no_audio} too_short={n_skipped_too_short}")


if __name__ == "__main__":
    main()
