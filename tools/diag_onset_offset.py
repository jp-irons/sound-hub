"""
diag_onset_offset.py — One-off diagnostic for the ~150ms onset-refine offset bug.

Not part of the regular clap-sync tooling — throwaway script to answer one
question: when detect_onset() (global-peak picker) and run_clap_test.py's
onset-refine path report an ambiguous ~150ms offset, while --no-onset-refine
(whole-buffer correlation) reports a confident sub-millisecond lag, WHERE in
node A's buffer is the genuine clap relative to whichever sample
detect_onset() actually picked?

Approach: pull the full coarse window from BOTH nodes (same as
--no-onset-refine), then instead of trusting detect_onset()'s single global-
max pick, find the top-K local maxima of A's smoothed energy envelope
(well-separated in time), and for each candidate slice a short window around
it from both A and B's already-downloaded buffers and run the same short-
window cross-correlation used by the onset-refine path. Whichever candidate
gives a strong, unambiguous correlation (high quality ratio, low lag) is the
real clap. Compare its time + amplitude against detect_onset()'s actual pick.

Usage:
    python tools/diag_onset_offset.py soundcapture160 soundcapture170 \\
        --hub-url http://192.168.101.220:8000 --username admin --password secret

Requires: numpy, scipy, requests. Run from sound-hub root.
"""

import argparse
import getpass
import os
import sys
import time

import numpy as np

from pull_two_nodes import _login, _pull_and_poll
from clap_sync_check import detect_onset, _correlate_and_report, _load_mono, _ONSET_WINDOW_MS

CANDIDATE_MIN_SEPARATION_MS = 50.0  # candidates closer together than this are treated as the same event
TOP_K = 6
SLICE_HALF_WINDOW_SECS = 0.15  # matches run_clap_test.py's default --onset-window-secs / 2


def _download(hub_url: str, hub_relative_path: str, dest_path: str) -> None:
    import requests
    url = f"{hub_url}/{hub_relative_path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)


def _pull_and_download(hub_url, node_id, t_start_us, t_end_us, poll_secs, headers, out_dir):
    _, r = _pull_and_poll(hub_url, node_id, t_start_us, t_end_us, poll_secs, headers)
    if r.get("_error"):
        print(f"  [ERROR] {node_id}: {r['_error']}")
        sys.exit(1)
    acks = r.get("acks") or []
    status = acks[-1]["status"] if acks else None
    if r.get("file"):
        hub_relative_path = f"audio/{r['file']}"
        local_path = os.path.join(out_dir, r["file"])
        _download(hub_url, hub_relative_path, local_path)
        print(f"  [OK] {node_id}: {local_path}  ({r.get('bytes', '?')} bytes)")
        return local_path
    elif status == "unavailable":
        print(f"  [UNAVAILABLE] {node_id}")
        sys.exit(1)
    elif r.get("_timeout"):
        print(f"  [TIMEOUT] {node_id}")
        sys.exit(1)
    else:
        print(f"  [ERROR] {node_id}: unexpected response: {r}")
        sys.exit(1)


def _smoothed_rms(data: np.ndarray, rate: int, window_ms: float = _ONSET_WINDOW_MS) -> np.ndarray:
    win = max(1, int(round(window_ms * 1e-3 * rate)))
    energy = np.convolve(data.astype(np.float64) ** 2, np.ones(win) / win, mode="same")
    return np.sqrt(energy)


def _top_k_local_maxima(rms: np.ndarray, rate: int, k: int, min_sep_ms: float):
    """Greedy top-K: take the global max, zero out a window around it, repeat."""
    min_sep = max(1, int(round(min_sep_ms * 1e-3 * rate)))
    work = rms.copy()
    candidates = []
    for _ in range(k):
        idx = int(np.argmax(work))
        if work[idx] <= 0:
            break
        candidates.append((idx, rms[idx]))
        lo, hi = max(0, idx - min_sep), min(len(work), idx + min_sep + 1)
        work[lo:hi] = -1.0
    return candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("node_a")
    parser.add_argument("node_b")
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--lead", type=float, default=20.0)
    parser.add_argument("--margin-secs", type=float, default=2.0)
    parser.add_argument("--hub-url", default="http://localhost:8000")
    parser.add_argument("--poll-secs", type=float, default=45.0)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--out-dir", default=".")
    parser.add_argument("--keep-wavs", action="store_true")
    args = parser.parse_args()

    if args.token:
        token = args.token
    else:
        username = args.username or input("Hub username: ")
        password = args.password or getpass.getpass("Hub password: ")
        token = _login(args.hub_url, username, password)
    headers = {"Authorization": f"Bearer {token}"}

    now_us = int(time.time() * 1_000_000)
    t_start_us = now_us - int((args.lead + args.margin_secs) * 1_000_000)
    t_end_us = t_start_us + int(args.duration * 1_000_000)

    print("--- Onset-offset diagnostic (whole coarse window from both nodes) ---")
    print(f"  Hub : {args.hub_url}   A={args.node_a}  B={args.node_b}")
    print()

    print(f"  Pulling node A ({args.node_a}) ...")
    path_a = _pull_and_download(args.hub_url, args.node_a, t_start_us, t_end_us,
                                 args.poll_secs, headers, args.out_dir)
    print(f"  Pulling node B ({args.node_b}) ...")
    path_b = _pull_and_download(args.hub_url, args.node_b, t_start_us, t_end_us,
                                 args.poll_secs, headers, args.out_dir)

    rate, a = _load_mono(path_a)
    rate_b, b = _load_mono(path_b)
    if rate != rate_b:
        print(f"  [ERROR] sample rate mismatch: A={rate} B={rate_b}")
        sys.exit(1)

    # What detect_onset() actually picks today (the "buggy" path).
    try:
        current_pick_idx = detect_onset(a, rate)
    except ValueError as e:
        print(f"  [ERROR] detect_onset() found nothing: {e}")
        sys.exit(1)

    rms = _smoothed_rms(a, rate)
    candidates = _top_k_local_maxima(rms, rate, TOP_K, CANDIDATE_MIN_SEPARATION_MS)

    half_n = int(round(SLICE_HALF_WINDOW_SECS * rate))

    print()
    print(f"  detect_onset() current pick: sample {current_pick_idx} "
          f"({current_pick_idx / rate:.3f}s into A's window)")
    print()
    print(f"  Top {len(candidates)} energy peaks in A (by amplitude, >= "
          f"{CANDIDATE_MIN_SEPARATION_MS:.0f}ms apart) vs. their short-window "
          f"correlation against B:")
    print()
    header = f"  {'rank':<5}{'sample':<10}{'t (s)':<10}{'rel.amp':<10}{'Δ vs pick':<14}{'lag (us)':<14}{'quality':<10}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    peak_amp = max(amp for _, amp in candidates) if candidates else 1.0

    for rank, (idx, amp) in enumerate(candidates, start=1):
        lo, hi = max(0, idx - half_n), min(len(a), idx + half_n)
        a_slice = a[lo:hi]
        b_lo, b_hi = max(0, idx - half_n), min(len(b), idx + half_n)
        b_slice = b[b_lo:b_hi]
        n = min(len(a_slice), len(b_slice))
        if n < rate * 0.05:  # need at least 50ms of overlap to bother
            continue
        a_slice, b_slice = a_slice[:n], b_slice[:n]

        from scipy.signal import correlate
        corr = correlate(a_slice - a_slice.mean(), b_slice - b_slice.mean(), mode="full")
        peak_idx = int(np.argmax(corr))
        lag_samples = peak_idx - (len(b_slice) - 1)
        lag_us = lag_samples * 1e6 / rate

        # quality ratio inline (avoid importing private helper twice with different shapes)
        exclude = max(1, int(round(1.0e-3 * rate)))
        masked = corr.astype(np.float64).copy()
        elo, ehi = max(0, peak_idx - exclude), min(len(corr), peak_idx + exclude + 1)
        masked[elo:ehi] = -np.inf
        second_val = masked[int(np.argmax(masked))]
        quality = (corr[peak_idx] / second_val) if (np.isfinite(second_val) and second_val > 0) else float("inf")

        delta_ms = (idx - current_pick_idx) / rate * 1000.0
        marker = "  <- detect_onset() pick" if idx == current_pick_idx else ""
        q_str = f"{quality:.2f}x" if np.isfinite(quality) else "inf"
        print(f"  {rank:<5}{idx:<10}{idx/rate:<10.3f}{amp/peak_amp:<10.2f}{delta_ms:+<13.1f}{lag_us:<+14.1f}{q_str:<10}{marker}")

    print()
    print("  Interpretation: the candidate with the best (highest) quality ratio AND a lag")
    print("  near zero (matching the confident sub-ms result you already saw from")
    print("  --no-onset-refine) is almost certainly the real clap. Compare its rank/amplitude")
    print("  and 'Δ vs pick' to detect_onset()'s current choice to see what's stealing the pick.")

    if not args.keep_wavs:
        for p in (path_a, path_b):
            try:
                os.remove(p)
            except OSError:
                pass


if __name__ == "__main__":
    main()
