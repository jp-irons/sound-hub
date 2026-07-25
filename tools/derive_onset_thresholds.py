"""
derive_onset_thresholds.py — Derive per-species onset_threshold_factor from
Xeno-canto reference recordings in test/samples/.

Why this exists:
    species_tdoa_params.onset_threshold_factor (the multiple of background
    RMS a call's energy-envelope peak must exceed to register as a real
    transient — see server/onset_detection.py's detect_onset()) was, for
    the 4 species tuned on 2026-07-13, derived ad hoc: measure a "ratio"
    (peak envelope RMS / background RMS, the same quantity detect_onset()
    thresholds against) per isolated call, take the median per species,
    then pick a threshold_factor as a judgment-call reduction below that —
    no fixed formula, no committed script (see git commit 4caf479 and
    project_soundhub_species_band_derivation memory for the full
    reconstruction of that method). This automates the same measurement
    across all 8 species using the segmentation infrastructure already
    built for derive_species_bands.py, and applies one uniform,
    documented safety cut instead of ad hoc per-species judgment calls.

    Deliberately report-only, same as derive_species_bands.py: never writes
    config/species_tdoa_params.json.

Why a uniform safety cut, not species-specific scaling:
    The only real field data point available (a 2026-07-11 Grey Butcherbird
    detection scoring 4.3x background RMS, well below even its eventual
    tuned value of 6.0) predates bandpass filtering going live
    (2026-07-11, same day, but after that particular measurement) —
    bandpass is supposed to improve effective SNR, so that data point isn't
    a trustworthy per-species correction factor going forward, only
    evidence that clean-recording-derived ratios run optimistic in
    general. A single modest uniform cut (--safety-cut, default 0.20)
    applied to every species' own median ratio is a more honest
    reflection of "we know this method runs optimistic by some amount, we
    don't yet know precisely how much" than pretending one stale,
    pre-bandpass data point justifies species-specific corrections.

Methodology:
    Reuses derive_species_bands.py's dedup (catalog + content) and
    segment_calls() to find call boundaries per file. For each found call,
    extracts a window padded by --ratio-window-pad-ms (default 500.0,
    matching the current window_margin_pre/post_ms default) on each side —
    deliberately sized to resemble a real production TDOA pull's scale,
    not the whole multi-call source recording, since detect_onset_us() in
    production only ever sees a narrow pulled clip, and background
    estimated over a whole multi-minute reference recording (mostly quiet
    gaps between calls) would read very differently from background
    estimated over a narrow clip dominated by the call itself. Computes
    the energy envelope over that window using the SAME window_ms
    (3.0ms) as server/onset_detection.py's detect_onset(), for maximum
    fidelity to what the production detector actually sees. ratio =
    envelope peak / envelope median, matching detect_onset()'s own
    background = np.median(rms) definition exactly.

Usage:
    python tools/derive_onset_thresholds.py
    python tools/derive_onset_thresholds.py --species "Gray Butcherbird" --safety-cut 0.25

Requires: numpy, scipy, ffmpeg on PATH. Run from the sound-hub root
directory (imports derive_species_bands.py and clap_sync_check.py from
the same tools/ dir).
"""

import argparse
import json
import os
import shutil
import tempfile

import numpy as np

from clap_sync_check import _energy_envelope
from derive_species_bands import (
    DEFAULT_EXCLUDE, SAMPLES_DIR, TARGET_RATE,
    dedupe_by_catalog, dedupe_by_content, load_all, segment_calls,
)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "species_tdoa_params.json")

# Matches server/onset_detection.py's _ONSET_WINDOW_MS exactly — this
# script's whole point is measuring what the production detector would see,
# so this constant must track that file, not be tuned independently.
_ONSET_WINDOW_MS = 3.0

# species_tdoa_params.onset_threshold_factor has a gt=2.0 floor in
# models.py (guards against an operator zeroing it out by accident) — warn
# loudly if a proposed value gets uncomfortably close, rather than let a
# silent clip-to-floor happen later at import time.
_FLOOR_WARN_MARGIN = 1.0


def _load_current_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH) as f:
        return json.load(f).get("species", {})


def ratios_for_file(rate: int, data: np.ndarray, seg_kwargs: dict, pad_ms: float) -> list:
    """Segment one already-loaded recording, and for each found call return
    the peak/median-background ratio of a production-scale window padded
    around it. Returns a flat list of ratios (one per call)."""
    segments = segment_calls(rate=rate, data=data, **seg_kwargs)
    pad = int(round(pad_ms * 1e-3 * rate))
    ratios = []
    for start, end in segments:
        w_start = max(0, start - pad)
        w_end = min(len(data), end + pad)
        window = data[w_start:w_end]
        if len(window) < 2:
            continue
        env = _energy_envelope(window, rate, _ONSET_WINDOW_MS)
        background = float(np.median(env))
        peak = float(np.max(env))
        if background <= 0:
            continue
        ratios.append(peak / background)
    return ratios


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples-dir", default=SAMPLES_DIR)
    parser.add_argument("--species", default=None,
                         help="Comma-separated species subset (default: every subdir of "
                              "--samples-dir except --exclude)")
    parser.add_argument("--exclude", default=",".join(DEFAULT_EXCLUDE))
    parser.add_argument("--threshold-factor", type=float, default=6.0,
                         help="Call-segmentation threshold-factor (finding calls, not the "
                              "onset_threshold_factor being derived) -- matches "
                              "derive_species_bands.py's default for consistency")
    parser.add_argument("--background-pct", type=float, default=20.0)
    parser.add_argument("--window-ms", type=float, default=20.0)
    parser.add_argument("--min-duration-ms", type=float, default=80.0)
    parser.add_argument("--merge-gap-ms", type=float, default=150.0)
    parser.add_argument("--pad-ms", type=float, default=30.0,
                         help="Segmentation edge padding -- see derive_species_bands.py")
    parser.add_argument("--ratio-window-pad-ms", type=float, default=500.0,
                         help="Padding around each found call for the RATIO measurement window "
                              "(separate from --pad-ms) -- sized to resemble a real production "
                              "pull, see module docstring")
    parser.add_argument("--safety-cut", type=float, default=0.20,
                         help="Uniform fractional reduction applied to each species' median "
                              "ratio to get the proposed onset_threshold_factor (default 0.20 "
                              "= use 80%% of the observed median ratio)")
    parser.add_argument("--tmp-dir", default=None)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    exclude = {s.strip() for s in args.exclude.split(",") if s.strip()}
    if args.species:
        species_list = [s.strip() for s in args.species.split(",") if s.strip()]
    else:
        species_list = sorted(
            d for d in os.listdir(args.samples_dir)
            if os.path.isdir(os.path.join(args.samples_dir, d)) and d not in exclude
        )

    current_config = _load_current_config()
    seg_kwargs = dict(
        window_ms=args.window_ms, threshold_factor=args.threshold_factor,
        background_pct=args.background_pct, min_duration_ms=args.min_duration_ms,
        merge_gap_ms=args.merge_gap_ms, pad_ms=args.pad_ms,
    )

    own_tmp = args.tmp_dir is None
    tmp_root = args.tmp_dir or tempfile.mkdtemp(prefix="derive_onset_thresholds_")
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

            all_ratios = []
            for path in kept:
                rate, data, duration = loaded[path]
                ratios = ratios_for_file(rate, data, seg_kwargs, args.ratio_window_pad_ms)
                print(f"  {os.path.basename(path)}: {len(ratios)} call(s) ratio-scored")
                all_ratios.extend(ratios)

            if not all_ratios:
                print(f"  [FLAG] no calls scored for {species} -- cannot derive a threshold.")
                full_report[species] = None
                continue

            median_ratio = float(np.median(all_ratios))
            proposed = median_ratio * (1.0 - args.safety_cut)

            print(f"  --- {species} summary ---")
            print(f"  {len(all_ratios)} call(s) ratio-scored")
            if len(all_ratios) < 15:
                print(f"  [warn] only {len(all_ratios)} call(s) -- treat this estimate as low-confidence.")
            print(f"  median observed ratio: {median_ratio:.2f}")
            print(f"  proposed onset_threshold_factor ({(1-args.safety_cut)*100:.0f}% of median, "
                  f"i.e. {args.safety_cut*100:.0f}% safety cut): {proposed:.2f}")
            if proposed < 2.0 + _FLOOR_WARN_MARGIN:
                print(f"  [FLAG] proposed value ({proposed:.2f}) is close to the models.py "
                      f"gt=2.0 floor -- review before applying.")

            current = current_config.get(species)
            if current:
                cur_factor = current.get("onset_threshold_factor")
                print(f"  Current config value: {cur_factor}")
                if cur_factor is not None:
                    print(f"  Delta: {proposed - cur_factor:+.2f}")
            else:
                print(f"  Current config value: (none -- factory default 8.0)")

            full_report[species] = {
                "n_calls": len(all_ratios), "median_ratio": median_ratio,
                "proposed_threshold_factor": proposed,
            }
    finally:
        if own_tmp:
            shutil.rmtree(tmp_root, ignore_errors=True)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(full_report, f, indent=2)
        print(f"\nWrote full report to {args.json_out}")

    print("\nReport only -- config/species_tdoa_params.json was not modified. "
          "Review before transcribing values in, same as derive_species_bands.py.")


if __name__ == "__main__":
    main()
