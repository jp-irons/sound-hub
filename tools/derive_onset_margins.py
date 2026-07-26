"""
derive_onset_margins.py — Derive per-species window_margin_pre_ms/
window_margin_post_ms from Xeno-canto reference recordings in test/samples/.

Why this exists:
    species_tdoa_params.window_margin_pre_ms/window_margin_post_ms (see
    routes.py's _plan_tdoa_attempt_inner / _pull_window_for_node) size how
    much audio to request around a detected onset instant, on top of the
    per-pair NodeTransit geometric floor -- currently a flat 500ms/500ms for
    every species, never data-derived. Two earlier approaches were discussed
    and rejected before this one (see project memory / conversation this
    followed):

    1. Measuring a full rise-time (background -> peak) directly from real
       *field* tdoa_attempt_nodes pulls. Rejected: a field pull's WAV is
       already cropped to the current window_margin_pre/post_ms, so any
       call whose true rise started earlier than that window is silently
       truncated -- you can't measure "how much lead-in is needed" from a
       buffer that's already too short to contain it. This is a
       left/right-censoring problem, not a sample-size problem; more field
       data does not fix it.
    2. Extrapolating a straight line from the steepest local slope
       (_refine_to_steepest_rise's own attack point) down to background.
       Avoids the censoring problem (only needs a few ms around the
       steepest point, always well inside any buffer that passed onset
       detection at all) but assumes the attack is linear all the way to
       background. Real transients usually have a shallower "toe" before
       the steep part, so a straight-line extrapolation from the steepest
       tangent would under-estimate the true lead-in needed -- optimistic,
       the dangerous direction.

    This script instead finds the actual lead-in/lead-out "knee" -- the
    real point where the energy envelope flattens into background, walking
    outward from the detected onset -- and proposes that measured distance,
    UNSCALED, as species_tdoa_params.window_margin_pre_ms/post_ms (changed
    2026-07-25 -- this script used to bake a 2x safety factor into its
    proposed value; that factor is now applied downstream, at the point of
    use, in routes.py's _pull_window_for_node -- see that function's
    comments -- because correlation.py's leading-edge correlation step also
    needs this same per-species figure, and it needs the raw, unscaled knee
    distance rather than an already-doubled pull-sizing value). It runs
    against the same Xeno-canto reference
    recordings already used for band/threshold derivation rather than field
    pulls, specifically because those files are NOT pre-cropped to any
    production pull window: there are often several real seconds of genuine
    quiet between calls, giving the knee-search room to actually find a
    real answer instead of hitting a truncated buffer edge. Any call where
    the search still runs out of room (a neighbouring call sits close by,
    or the file itself starts/ends too soon) is marked censored rather than
    silently treated as if the search boundary were the knee -- an
    unmeasured call must not quietly bias the aggregate downward.

    Clean-recording measurements bias long, not short, for this particular
    quantity -- worth noting since a clean-recording-derived metric was
    already tried once this session (tools/derive_onset_thresholds.py) and
    abandoned as unreliable. That was measuring peak/background RATIO,
    which is highly sensitive to the recording's own noise floor and window
    size. This measures how long the call's OWN attack/decay physically
    takes, which is mostly a property of the vocalization, not the
    recording -- though a quieter reference recording can reveal a longer,
    fainter pre-onset ramp than a noisier field recording would ever show
    for the same call (a fainter signal is undetectable once field noise is
    high enough to bury it), so if anything this over-estimates the true
    field requirement rather than under-estimating it. That's the safe
    direction to be wrong in, unlike the abandoned ratio approach.

    Deliberately report-only, same convention as derive_species_bands.py
    and derive_onset_thresholds.py: never writes
    config/species_tdoa_params.json.

Methodology per call:
    1. Reuses derive_species_bands.py's dedup (catalog + content) and
       segment_calls() to find call boundaries in the full, uncropped
       recording.
    2. Within each segment, finds the loudest point on a fine (3.0ms,
       matching server/onset_detection.py's _ONSET_WINDOW_MS) energy
       envelope and refines it to the steepest-rise sample -- the same
       onset anchor production's detect_onset() would compute if this
       exact segment were pulled as a clip.
    3. Walks the fine envelope backward from onset toward the previous
       segment's end (or the file start), and forward toward the next
       segment's start (or the file end) -- each additionally bounded by
       --max-lookback-ms/--max-lookahead-ms as a sanity cap. The local
       background for this specific gap is the median envelope value
       across whatever room is available to search. The knee is the point
       closest to onset where the envelope drops to
       --knee-threshold-factor x that local background.
    4. If the walk exhausts its search room before crossing that level, the
       call is censored for that direction -- excluded from the proposed
       margin, but counted, with its cause reported separately (ran into a
       neighbouring call/file edge, vs. hit the --max-lookback-ms/
       --max-lookahead-ms cap) -- a cap-limited censor can be fixed by just
       raising that flag and re-running; a neighbour-limited one is a real
       property of that recording.
    5. Per call, the measured knee distance is used as-is (no scaling). Per
       species, reports the percentile distribution of that across all
       uncensored calls and proposes --margin-pct (default 75th percentile)
       of it as the candidate window_margin_pre_ms/post_ms -- the pull
       request itself applies a 2x safety factor on top of this value at
       the point of use (see routes.py), not here.

Usage:
    python tools/derive_onset_margins.py
    python tools/derive_onset_margins.py --species "Gray Butcherbird"
    python tools/derive_onset_margins.py --max-lookback-ms 5000 --max-lookahead-ms 5000

Requires: numpy, scipy, ffmpeg on PATH. Run from the sound-hub root
directory (imports derive_species_bands.py and clap_sync_check.py from the
same tools/ dir).
"""

import argparse
import json
import os
import shutil
import tempfile

import numpy as np

from clap_sync_check import _energy_envelope, _refine_to_steepest_rise
from derive_species_bands import (
    DEFAULT_EXCLUDE, SAMPLES_DIR, dedupe_by_catalog, dedupe_by_content,
    load_all, load_current_config, segment_calls,
)

# Matches server/onset_detection.py's _ONSET_WINDOW_MS exactly -- this is
# the fine envelope used to locate the onset/knee itself, distinct from
# segment_calls()'s own coarser 20ms envelope used only to find call
# boundaries in the first place.
_ONSET_WINDOW_MS = 3.0

# Matches server/onset_detection.py's _ONSET_REFINE_MARGIN_MS.
_ONSET_REFINE_MARGIN_MS = 5.0


def _onset_idx_for_segment(data: np.ndarray, fine_env: np.ndarray, rate: int,
                            start: int, end: int, refine_margin_samples: int) -> int:
    """Loudest point of the fine envelope within [start, end), refined to
    the steepest-rise raw sample -- the same anchor production's
    detect_onset() would compute for this exact segment if it were pulled
    as a standalone clip."""
    peak_idx = start + int(np.argmax(fine_env[start:end]))
    return _refine_to_steepest_rise(data, peak_idx, refine_margin_samples)


def _find_knee_backward(env: np.ndarray, onset_idx: int, bound_idx: int,
                         background: float, knee_threshold_factor: float) -> tuple:
    """Lead-in knee: walk from onset_idx back toward bound_idx (< onset_idx).
    Returns (knee_idx, censored) -- the index closest to onset_idx where env
    drops to <= knee_threshold_factor x background, or (bound_idx, True) if
    it never does before running out of room to search."""
    window = env[bound_idx:onset_idx]
    below = np.where(window <= knee_threshold_factor * background)[0]
    if below.size == 0:
        return bound_idx, True
    return bound_idx + int(below[-1]), False


def _find_knee_forward(env: np.ndarray, onset_idx: int, bound_idx: int,
                        background: float, knee_threshold_factor: float) -> tuple:
    """Lead-out knee: mirror of _find_knee_backward, walking forward from
    onset_idx toward bound_idx (> onset_idx)."""
    window = env[onset_idx:bound_idx]
    below = np.where(window <= knee_threshold_factor * background)[0]
    if below.size == 0:
        return bound_idx, True
    return onset_idx + int(below[0]), False


def measure_margins_for_file(path: str, rate: int, data: np.ndarray, seg_kwargs: dict,
                              knee_threshold_factor: float, max_lookback_ms: float,
                              max_lookahead_ms: float) -> tuple:
    """Segment one already-loaded recording and measure lead-in/lead-out
    knee distances per call. Returns (rows, warnings)."""
    segments = segment_calls(data=data, rate=rate, **seg_kwargs)
    if not segments:
        return [], ["    [warn] no candidate calls found -- recording may be too quiet, "
                     "or --threshold-factor is too high for it."]

    fine_env = _energy_envelope(data, rate, _ONSET_WINDOW_MS)
    refine_margin = max(1, int(round(_ONSET_REFINE_MARGIN_MS * 1e-3 * rate)))
    max_lookback = max(1, int(round(max_lookback_ms * 1e-3 * rate)))
    max_lookahead = max(1, int(round(max_lookahead_ms * 1e-3 * rate)))

    rows = []
    warnings = []
    for i, (start, end) in enumerate(segments):
        onset_idx = _onset_idx_for_segment(data, fine_env, rate, start, end, refine_margin)

        prev_end = segments[i - 1][1] if i > 0 else 0
        next_start = segments[i + 1][0] if i < len(segments) - 1 else len(data)
        cap_back = max(0, onset_idx - max_lookback)
        cap_fwd = min(len(data), onset_idx + max_lookahead)
        back_bound = max(prev_end, cap_back)
        fwd_bound = min(next_start, cap_fwd)
        back_limited_by_cap = back_bound == cap_back and cap_back > prev_end
        fwd_limited_by_cap = fwd_bound == cap_fwd and cap_fwd < next_start

        if onset_idx - back_bound < 2 or fwd_bound - onset_idx < 2:
            warnings.append(
                f"    [warn] segment {i} ({start/rate:.2f}-{end/rate:.2f}s): no room to search "
                f"for knees (adjacent call or file edge) -- skipped entirely"
            )
            continue

        back_background = float(np.median(fine_env[back_bound:onset_idx]))
        fwd_background = float(np.median(fine_env[onset_idx:fwd_bound]))

        knee_back_idx, back_censored = _find_knee_backward(
            fine_env, onset_idx, back_bound, back_background, knee_threshold_factor
        )
        knee_fwd_idx, fwd_censored = _find_knee_forward(
            fine_env, onset_idx, fwd_bound, fwd_background, knee_threshold_factor
        )

        rows.append({
            "file": os.path.basename(path),
            "onset_s": onset_idx / rate,
            "lead_in_ms": (onset_idx - knee_back_idx) / rate * 1000.0,
            "lead_in_censored": back_censored,
            "lead_in_censor_reason": (
                ("hit_cap" if back_limited_by_cap else "hit_recording") if back_censored else None
            ),
            "lead_out_ms": (knee_fwd_idx - onset_idx) / rate * 1000.0,
            "lead_out_censored": fwd_censored,
            "lead_out_censor_reason": (
                ("hit_cap" if fwd_limited_by_cap else "hit_recording") if fwd_censored else None
            ),
        })
    return rows, warnings


def aggregate_direction(rows: list, ms_key: str, censored_key: str, reason_key: str,
                         margin_pct: float) -> dict:
    """proposed_knee_ms = margin_pct percentile of each uncensored call's
    RAW measured knee distance (unscaled). Censored calls are excluded from
    the percentile entirely (not imputed, not treated as 0) -- see module
    docstring on why a censored call must not silently bias the result.

    No 2x safety factor applied here (changed 2026-07-25 -- see
    project_soundhub_margin_derivation memory's follow-up correction): this
    used to fold a 2x multiplier into the value proposed for
    species_tdoa_params.window_margin_pre/post_ms, matching how routes.py
    consumed it at the time (straight pull-window sizing). Since then
    window_margin_pre/post_ms has been redefined to store the raw knee
    distance itself -- the 2x pull-request safety factor is now applied at
    the point of use, in routes.py's _pull_window_for_node, rather than
    baked into this script's output -- because the correlation leading-edge
    step (correlation.py) also needs this same per-species figure, and it
    needs the UNSCALED knee distance (the call's actual extent), not a
    value that's already been doubled for a different consumer's purposes.
    One stored number, scaled differently by each downstream consumer, not
    two derived numbers that could drift apart."""
    found = [r for r in rows if not r[censored_key]]
    censored = [r for r in rows if r[censored_key]]
    n_cap = sum(1 for r in censored if r[reason_key] == "hit_cap")
    n_recording = sum(1 for r in censored if r[reason_key] == "hit_recording")
    result = {
        "n_found": len(found), "n_censored": len(censored),
        "n_censored_cap": n_cap, "n_censored_recording": n_recording,
    }
    if not found:
        result["proposed_margin_ms"] = None
        return result
    margins = np.array([r[ms_key] for r in found])
    percentiles = [50, 75, 90, 100]
    result["margin_percentiles_ms"] = {p: float(np.percentile(margins, p)) for p in percentiles}
    result["proposed_margin_ms"] = float(np.percentile(margins, margin_pct))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples-dir", default=SAMPLES_DIR)
    parser.add_argument("--species", default=None,
                         help="Comma-separated species subset (default: every subdir of "
                              "--samples-dir except --exclude)")
    parser.add_argument("--exclude", default=",".join(DEFAULT_EXCLUDE))
    parser.add_argument("--threshold-factor", type=float, default=6.0,
                         help="Call-segmentation threshold-factor (finding calls, not the "
                              "knee-detection threshold below) -- matches "
                              "derive_species_bands.py's default for consistency")
    parser.add_argument("--background-pct", type=float, default=20.0)
    parser.add_argument("--window-ms", type=float, default=20.0)
    parser.add_argument("--min-duration-ms", type=float, default=80.0)
    parser.add_argument("--merge-gap-ms", type=float, default=150.0)
    parser.add_argument("--pad-ms", type=float, default=30.0)
    parser.add_argument("--knee-threshold-factor", type=float, default=1.5,
                         help="Multiple of local background the envelope must drop to (walking "
                              "away from onset) to count as having reached the knee -- "
                              "deliberately much lower than onset_threshold_factor (~5-8), since "
                              "this is asking 'close to true floor', not 'call-worthy'")
    parser.add_argument("--max-lookback-ms", type=float, default=3000.0,
                         help="Sanity cap on how far back of onset to search for the lead-in "
                              "knee -- a censor caused by hitting this (vs. a neighbouring call) "
                              "can just be fixed by raising it and re-running")
    parser.add_argument("--max-lookahead-ms", type=float, default=3000.0,
                         help="Same as --max-lookback-ms, forward from onset for the lead-out knee")
    parser.add_argument("--margin-pct", type=float, default=75.0,
                         help="Percentile of the per-call raw (unscaled) knee-distance "
                              "distribution proposed as the new window_margin_pre_ms/post_ms -- "
                              "the pull request itself applies a 2x safety factor on top of "
                              "this at the point of use (routes.py), not here")
    parser.add_argument("--tmp-dir", default=None,
                         help="Scratch dir for mp3->wav conversions (default: a temp dir, cleaned up after)")
    parser.add_argument("--json-out", default=None, help="Optional path to dump the full structured report")
    args = parser.parse_args()

    exclude = {s.strip() for s in args.exclude.split(",") if s.strip()}
    if args.species:
        species_list = [s.strip() for s in args.species.split(",") if s.strip()]
    else:
        species_list = sorted(
            d for d in os.listdir(args.samples_dir)
            if os.path.isdir(os.path.join(args.samples_dir, d)) and d not in exclude
        )

    current_config = load_current_config()
    seg_kwargs = dict(
        window_ms=args.window_ms, threshold_factor=args.threshold_factor,
        background_pct=args.background_pct, min_duration_ms=args.min_duration_ms,
        merge_gap_ms=args.merge_gap_ms, pad_ms=args.pad_ms,
    )

    own_tmp = args.tmp_dir is None
    tmp_root = args.tmp_dir or tempfile.mkdtemp(prefix="derive_onset_margins_")
    full_report = {}

    try:
        for species in species_list:
            species_dir = os.path.join(args.samples_dir, species)
            files = [os.path.join(species_dir, f) for f in sorted(os.listdir(species_dir))
                     if f.lower().endswith((".mp3", ".wav"))]
            if not files:
                print(f"\n=== {species} === (no sample files found, skipping)")
                continue

            print(f"\n=== {species} ({len(files)} sample file(s)) ===")
            kept, dedupe_log = dedupe_by_catalog(files)
            for line in dedupe_log:
                print(line)

            species_tmp = os.path.join(tmp_root, species.replace(" ", "_"))
            try:
                loaded = load_all(kept, species_tmp)
            except RuntimeError as e:
                print(f"  [FLAG] {e}")
                continue

            kept, content_dedupe_log = dedupe_by_content(loaded)
            for line in content_dedupe_log:
                print(line)

            all_rows = []
            for path in kept:
                rate, data, duration = loaded[path]
                rows, warnings = measure_margins_for_file(
                    path, rate, data, seg_kwargs, args.knee_threshold_factor,
                    args.max_lookback_ms, args.max_lookahead_ms,
                )
                print(f"  {os.path.basename(path)}: {len(rows)} call(s) measured")
                for w in warnings:
                    print(w)
                all_rows.extend(rows)

            if not all_rows:
                print(f"  [FLAG] no calls measured for {species} -- cannot derive margins.")
                full_report[species] = None
                continue

            pre_agg = aggregate_direction(all_rows, "lead_in_ms", "lead_in_censored",
                                           "lead_in_censor_reason", args.margin_pct)
            post_agg = aggregate_direction(all_rows, "lead_out_ms", "lead_out_censored",
                                            "lead_out_censor_reason", args.margin_pct)
            full_report[species] = {"rows": all_rows, "pre": pre_agg, "post": post_agg}

            print(f"  --- {species} summary ({len(all_rows)} call(s) total) ---")
            for label, agg, cap_name in (
                ("lead-in / pre-margin", pre_agg, "--max-lookback-ms"),
                ("lead-out / post-margin", post_agg, "--max-lookahead-ms"),
            ):
                print(f"  {label}: {agg['n_found']} found, {agg['n_censored']} censored "
                      f"({agg['n_censored_cap']} hit {cap_name} cap, "
                      f"{agg['n_censored_recording']} hit a neighbouring call/file edge)")
                if agg["proposed_margin_ms"] is None:
                    print(f"    no uncensored calls -- cannot propose a value.")
                    continue
                if agg["n_found"] < 10:
                    print(f"    [warn] only {agg['n_found']} uncensored call(s) -- treat this as low-confidence.")
                mp = agg["margin_percentiles_ms"]
                print(f"    knee-distance percentiles (ms, unscaled): 50%={mp[50]:.0f}  75%={mp[75]:.0f}  "
                      f"90%={mp[90]:.0f}  max={mp[100]:.0f}")
                print(f"    Proposed ({args.margin_pct:.0f}th pct, unscaled): {agg['proposed_margin_ms']:.0f}ms")

            current = current_config.get(species)
            if current:
                cur_pre = current.get("window_margin_pre_ms")
                cur_post = current.get("window_margin_post_ms")
                print(f"  Current config value: pre={cur_pre}ms post={cur_post}ms")
                if cur_pre is not None and pre_agg["proposed_margin_ms"] is not None:
                    print(f"  Delta: pre {pre_agg['proposed_margin_ms'] - cur_pre:+.0f}ms")
                if cur_post is not None and post_agg["proposed_margin_ms"] is not None:
                    print(f"  Delta: post {post_agg['proposed_margin_ms'] - cur_post:+.0f}ms")
            else:
                print(f"  Current config value: (none -- new species)")
    finally:
        if own_tmp:
            shutil.rmtree(tmp_root, ignore_errors=True)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(full_report, f, indent=2)
        print(f"\nWrote full report to {args.json_out}")

    print("\nReport only -- config/species_tdoa_params.json was not modified. "
          "Review the numbers above (especially censored fractions) before transcribing them in.")


if __name__ == "__main__":
    main()
