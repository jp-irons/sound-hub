"""
run_clap_test.py — One-shot clap-sync test: pull, download, correlate.

Combines pull_two_nodes.py (request a window from a node, poll until ready)
and clap_sync_check.py (cross-correlate two WAVs to measure inter-node sync
error) into a single command, so a clap test no longer requires copy-pasting
filenames between separate steps.

Default mode ("onset-refine"): pulls a full coarse window from node A only,
locally detects the clap's onset in A's audio, then pulls a short precise
window from node B centered on that onset and correlates against the
matching slice of A's already-downloaded buffer. This avoids correlating
across the whole multi-second buffer — where an unrelated noise event, or a
room echo of the clap itself, can outscore the real direct-path arrival —
and is also a stand-in for the node-side "is this segment worth sending to
the hub" decision the project will eventually need for live bird-call
triggering. Pass --no-onset-refine to fall back to pulling the full window
from both nodes and correlating directly, for comparison.

Usage:
    python tools/run_clap_test.py soundcapture160 soundcapture170 \\
        --username admin --password secret

    # Longer coarse window, further back, saving an alignment plot:
    python tools/run_clap_test.py nodeA nodeB --duration 30 --lead 10 \\
        --hub-url http://192.168.101.220:8000 --username admin --password secret \\
        --plot clap.png

    # Skip login and use an existing bearer token instead:
    python tools/run_clap_test.py nodeA nodeB --token eyJ...

    # Keep the downloaded WAVs instead of deleting them after the test:
    python tools/run_clap_test.py nodeA nodeB --keep-wavs

    # Old whole-window-both-nodes behavior, for comparison:
    python tools/run_clap_test.py nodeA nodeB --no-onset-refine

Run from the sound-hub root directory. Requires: requests, numpy, scipy (see
tools/requirements.txt). matplotlib optional, only needed for --plot.
"""

import argparse
import getpass
import os
import sys
import time

import requests

# Both modules live alongside this script in tools/ — import their pieces
# directly rather than re-implementing pull/poll/correlate/onset logic here.
import numpy as np

from pull_two_nodes import _login, _pull_and_poll
from clap_sync_check import (
    analyze, detect_onset_candidates, _correlate_and_report, _score_correlation, _load_mono,
    trim_to_leading_edge, _MIN_PEAK_CORR_COEF,
)


def _download(hub_url: str, hub_relative_path: str, dest_path: str) -> None:
    url = f"{hub_url}/{hub_relative_path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)


def _pull_and_download(hub_url: str, node_id: str, t_start_us: int, t_end_us: int,
                        poll_secs: float, headers: dict, out_dir: str, fatal: bool = True):
    """Request a window from one node, poll until ready, download the WAV,
    and return its local path. On error/timeout/unavailable: exits the
    process if fatal=True (the default — used where there's no fallback,
    e.g. node A's only pull), or prints a warning and returns None if
    fatal=False (used when trying multiple onset candidates against node B,
    where one candidate failing just means trying the next one).
    """
    def _fail(msg: str):
        print(msg)
        if fatal:
            sys.exit(1)
        return None

    _, r = _pull_and_poll(hub_url, node_id, t_start_us, t_end_us, poll_secs, headers)
    if r.get("_error"):
        return _fail(f"  [ERROR] {node_id}: {r['_error']}")
    acks = r.get("acks") or []
    status = acks[-1]["status"] if acks else None
    if r.get("file"):
        hub_relative_path = f"audio/{r['file']}"
        local_path = os.path.join(out_dir, r["file"])
        _download(hub_url, hub_relative_path, local_path)
        print(f"  [OK] {node_id}: {local_path}  ({r.get('bytes', '?')} bytes)")
        return local_path
    elif status == "unavailable":
        return _fail(f"  [UNAVAILABLE] {node_id}: node has no audio for that window "
                      f"(GPS locked? on SD? within stored history?)")
    elif r.get("_timeout"):
        return _fail(f"  [TIMEOUT] {node_id}: no result within {poll_secs}s")
    else:
        return _fail(f"  [ERROR] {node_id}: unexpected response: {r}")


def main():
    parser = argparse.ArgumentParser(
        description="Pull a matching audio window from two nodes and measure inter-node "
                     "clap-sync error in one command.")
    parser.add_argument("node_a", help="Hub node ID for node A")
    parser.add_argument("node_b", help="Hub node ID for node B")
    parser.add_argument("--duration", type=float, default=20.0,
                         help="Length of the coarse audio window in seconds (default 20). With "
                              "--onset-refine (default), only node A is pulled for this full "
                              "duration; node B only gets the short --onset-window-secs slice.")
    parser.add_argument("--lead", type=float, default=20.0,
                         help="How far before 'now' the window starts, in seconds (default 20, "
                              "i.e. window is [now-20s, now-20s+duration]).")
    parser.add_argument("--margin-secs", type=float, default=2.0,
                         help="Extra safety margin (seconds) subtracted on top of --lead so the "
                              "window's end lands safely before 'now' (default 2.0). Without this, "
                              "a window ending at or near 'now' can have its tail clipped by the "
                              "node's live ring buffer — there's no captured audio for a window end "
                              "that hasn't happened yet from the node's own GPS-corrected clock's "
                              "point of view (PC/node clock offset, network delay, processing time "
                              "all eat into this). Set to 0 to restore the old exact-'now' behavior.")
    parser.add_argument("--onset-refine", dest="onset_refine", action="store_true", default=True,
                         help="(Default) Detect the clap's onset in node A's coarse pull, then pull "
                              "only a short window around it from node B, and correlate that "
                              "against the matching slice of A — instead of correlating the full "
                              "coarse window from both nodes. Avoids picking up an unrelated noise "
                              "event or room echo elsewhere in a long buffer.")
    parser.add_argument("--no-onset-refine", dest="onset_refine", action="store_false",
                         help="Disable onset refinement: pull the full coarse window from both "
                              "nodes and correlate directly (old behavior).")
    parser.add_argument("--onset-window-secs", type=float, default=0.3,
                         help="Width (seconds) of the precise window pulled from node B and sliced "
                              "from node A around the detected onset (default 0.3s). Only used "
                              "with --onset-refine.")
    parser.add_argument("--onset-threshold", type=float, default=8.0,
                         help="Onset detector sanity-check threshold: the loudest transient found "
                              "must exceed this multiple of background RMS energy, or detection "
                              "fails rather than returning a meaningless index (default 8.0). Only "
                              "used with --onset-refine.")
    parser.add_argument("--onset-margin-ms", type=float, default=5.0,
                         help="+/- search margin (ms) around the detected energy peak used to "
                              "refine to the steepest-rise sample (default 5.0). Only used with "
                              "--onset-refine.")
    parser.add_argument("--onset-candidates", type=int, default=3,
                         help="Number of top energy peaks in node A's coarse window to test "
                              "against node B before picking the best match (default 3). "
                              "Trusting only the single loudest peak fails whenever a competing "
                              "transient (echo, wind, insects) is comparably loud to the real "
                              "clap; testing several candidates costs a few extra short pulls "
                              "from B but lets the actual cross-node correlation quality pick "
                              "the winner instead of guessing from amplitude alone. Only used "
                              "with --onset-refine.")
    parser.add_argument("--hub-url", default="http://localhost:8000",
                         help="Hub base URL (default http://localhost:8000).")
    parser.add_argument("--poll-secs", type=float, default=45.0,
                         help="How long to poll each request before giving up (default 45s).")
    parser.add_argument("--username", default=None,
                         help="Hub login username. Prompted if omitted and --token not given.")
    parser.add_argument("--password", default=None,
                         help="Hub login password. Prompted (hidden) if omitted and --token not given.")
    parser.add_argument("--token", default=None,
                         help="Use an existing bearer token instead of logging in with --username/--password.")
    parser.add_argument("--offset-us", type=float, default=0.0,
                         help="Known (tStart_b - tStart_a) in microseconds — not needed here since "
                              "this script always requests an identical window for both nodes, "
                              "but passed through to clap_sync_check for parity.")
    parser.add_argument("--plot", metavar="PNG_PATH", default=None,
                         help="Save an alignment plot to this path (requires matplotlib).")
    parser.add_argument("--keep-wavs", action="store_true",
                         help="Don't delete the downloaded WAV files after analysis.")
    parser.add_argument("--out-dir", default=".",
                         help="Directory to download WAVs into (default: current directory).")
    args = parser.parse_args()

    if args.token:
        token = args.token
    else:
        username = args.username or input("Hub username: ")
        password = args.password or getpass.getpass("Hub password: ")
        try:
            token = _login(args.hub_url, username, password)
        except requests.HTTPError as e:
            print(f"  [ERROR] login failed: {e}")
            sys.exit(1)
    headers = {"Authorization": f"Bearer {token}"}

    if args.lead < args.duration:
        print(f"  [ERROR] --lead ({args.lead}s) is less than --duration ({args.duration}s) — "
              f"the requested window would extend {args.duration - args.lead:.1f}s into the "
              f"future, which the nodes can't have captured yet at request time. This produces "
              f"inconsistent/short audio between the two nodes and meaningless correlation "
              f"results (e.g. multi-second 'lag'). Use --lead >= --duration so the window ends "
              f"at or before 'now'.")
        sys.exit(1)

    now_us = int(time.time() * 1_000_000)
    t_start_us = now_us - int((args.lead + args.margin_secs) * 1_000_000)
    t_end_us = t_start_us + int(args.duration * 1_000_000)

    fmt = lambda us: time.strftime("%H:%M:%S", time.gmtime(us / 1_000_000)) + f".{us % 1_000_000 // 1000:03d}"
    print("--- One-shot clap sync test ---")
    print(f"  Hub      : {args.hub_url}")
    print(f"  Window   : {t_start_us} .. {t_end_us}  [{fmt(t_start_us)} .. {fmt(t_end_us)} UTC]"
          f"  (margin {args.margin_secs:.1f}s before 'now')")
    print(f"  Node A   : {args.node_a}")
    print(f"  Node B   : {args.node_b}")
    print(f"  Mode     : {'onset-refine' if args.onset_refine else 'whole-window (legacy)'}")
    print()

    local_paths = {}

    if not args.onset_refine:
        # Legacy behavior: pull the full coarse window from both nodes in
        # parallel and correlate directly.
        from concurrent.futures import ThreadPoolExecutor
        print("  Pulling both nodes ...")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                args.node_a: pool.submit(_pull_and_download, args.hub_url, args.node_a,
                                          t_start_us, t_end_us, args.poll_secs, headers, args.out_dir),
                args.node_b: pool.submit(_pull_and_download, args.hub_url, args.node_b,
                                          t_start_us, t_end_us, args.poll_secs, headers, args.out_dir),
            }
            for node_id, fut in futures.items():
                local_paths[node_id] = fut.result()

        print()
        print("--- Cross-correlation ---")
        analyze(
            local_paths[args.node_a], local_paths[args.node_b],
            offset_us=args.offset_us, plot_path=args.plot,
        )
    else:
        # Onset-refine: pull A's coarse window, find the top candidate
        # transients locally, then test EACH one against a short precise
        # pull from B and keep whichever actually correlates well — instead
        # of trusting the single loudest peak in A, which has no defense
        # against a competing transient (echo, wind, insects) that happens
        # to be comparably loud to the real clap.
        print(f"  Pulling coarse window from node A ({args.node_a}) ...")
        local_paths[args.node_a] = _pull_and_download(
            args.hub_url, args.node_a, t_start_us, t_end_us, args.poll_secs, headers, args.out_dir)

        rate, a_full = _load_mono(local_paths[args.node_a])
        try:
            candidates = detect_onset_candidates(
                a_full, rate, k=args.onset_candidates,
                threshold_factor=args.onset_threshold, margin_ms=args.onset_margin_ms)
        except ValueError as e:
            print(f"  [ERROR] onset detection on node A failed: {e}")
            sys.exit(1)

        half_window_us = int(args.onset_window_secs * 1_000_000 / 2)
        print(f"  Testing {len(candidates)} candidate transient(s) in node A against node B "
              f"(loudest first) ...")
        print()

        scored = []  # (quality_ratio, peak_corr_coef, rank, onset_idx, a_slice, b_data, score, b_path)
        tried_b_paths = []
        for rank, onset_idx in enumerate(candidates, start=1):
            onset_utc_us = t_start_us + round(onset_idx * 1_000_000 / rate)
            nt_start_us = onset_utc_us - half_window_us
            nt_end_us = onset_utc_us + half_window_us
            print(f"  [{rank}/{len(candidates)}] sample {onset_idx} "
                  f"({onset_idx / rate:.3f}s into A's window) -> {fmt(onset_utc_us)} UTC")

            b_path = _pull_and_download(args.hub_url, args.node_b, nt_start_us, nt_end_us,
                                         args.poll_secs, headers, args.out_dir, fatal=False)
            if b_path is None:
                print("      skipped (pull failed)")
                print()
                continue
            tried_b_paths.append(b_path)
            _, b_data = _load_mono(b_path)

            # Slice the matching window out of A's already-downloaded coarse
            # buffer (see AudioStore::snapshotRange() center-anchoring note
            # in clap_sync_check.py's module docstring history for why this
            # lines up with B's pull to within a fraction of a sample).
            a_start_idx = round((nt_start_us - t_start_us) * rate / 1_000_000)
            a_end_idx = a_start_idx + len(b_data)
            if a_start_idx < 0 or a_end_idx > len(a_full):
                print("      skipped (window falls outside node A's coarse buffer — "
                      "candidate too close to the coarse window's edge)")
                print()
                continue
            a_slice = a_full[a_start_idx:a_end_idx]

            # a_slice and b_data are both centered on onset_idx by
            # construction (nt_start/nt_end are onset_utc_us +/- half_window),
            # so onset_idx's position within a_slice is just the same offset
            # from a_slice's start as it is from a_full's start, shifted by
            # a_start_idx. Score off a short window at that attack edge
            # rather than the full slice — see trim_to_leading_edge()'s
            # docstring for why the full slice's correlation is unreliable
            # (clap resonance ring-down, not a separate noise source).
            onset_in_slice = onset_idx - a_start_idx
            trimmed_a, trimmed_b = trim_to_leading_edge(a_slice, b_data, rate, onset_in_slice)
            score = _score_correlation(rate, trimmed_a, trimmed_b)
            q = score["quality_ratio"]
            coef = score["peak_corr_coef"]
            q_str = f"{q:.2f}x" if np.isfinite(q) else "inf"
            print(f"      lag {score['lag_us']:+.1f} us, quality {q_str}, corr coef {coef:.2f}")
            print()

            scored.append((q, coef, rank, onset_idx, a_slice, b_data, score, b_path))

        if not scored:
            print("  [ERROR] every candidate failed to pull or fell outside A's buffer — "
                  "no usable result. Try a larger --duration/--lead, or more --onset-candidates.")
            sys.exit(1)

        # Prefer candidates whose leading-edge correlation peak is strong
        # enough (relative to the slices' own energy) to trust at all, then
        # rank by quality_ratio among those. A candidate can score a
        # deceptively high or "inf" quality_ratio purely because its slice
        # has too little real signal for a competing second peak to exist —
        # see _MIN_PEAK_CORR_COEF's docstring in clap_sync_check.py. Only
        # fall back to ranking everything by quality_ratio (the old
        # behavior) if no candidate clears the trust bar at all.
        trusted = [c for c in scored if c[1] >= _MIN_PEAK_CORR_COEF]
        if trusted:
            best = max(trusted, key=lambda c: c[0])
        else:
            print("  [WARN] no candidate's correlation peak was strong enough to trust "
                  f"(all below corr coef {_MIN_PEAK_CORR_COEF}) — falling back to the highest "
                  "quality_ratio anyway, but treat this result as unreliable.")
            best = max(scored, key=lambda c: c[0])

        quality_ratio, peak_corr_coef, rank, onset_idx, a_slice, b_data, score, winning_b_path = best
        local_paths[args.node_b] = winning_b_path
        print(f"  Best match: candidate #{rank} of {len(candidates)} "
              f"(sample {onset_idx}, quality {quality_ratio:.2f}x, corr coef {peak_corr_coef:.2f})")
        print()
        print(f"--- Cross-correlation (onset-windowed, best of {len(candidates)} candidates) ---")
        _correlate_and_report(
            rate, a_slice, b_data, offset_us=args.offset_us, plot_path=args.plot, score=score,
        )

        # Non-winning B pulls aren't the result — drop them regardless of --keep-wavs.
        for p in tried_b_paths:
            if p != winning_b_path:
                try:
                    os.remove(p)
                except OSError:
                    pass

    if not args.keep_wavs:
        for p in local_paths.values():
            try:
                os.remove(p)
            except OSError:
                pass

    sys.exit(0)


if __name__ == "__main__":
    main()
