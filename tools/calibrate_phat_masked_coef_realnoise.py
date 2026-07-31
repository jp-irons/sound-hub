"""
calibrate_phat_masked_coef_realnoise.py — same calibration methodology as
calibrate_phat_masked_coef.py (synthetic ground truth: real Xeno-canto
call + known injected delay, split correct/incorrect by whether the
estimated lag lands within tolerance of the known truth), but with REAL
recorded field noise substituted for synthetic Gaussian noise.

WHY (2026-07-31): the Gaussian-noise calibration's candidate thresholds
(spotcheck_phat_masked_thresholds.py) badly under-predicted real-data
trust rates — only 2/24 real node pairs trusted at the "keep ~90% of
synthetic-correct-trials" threshold, not anywhere near 90%. Real
coefficients and quality ratios both landed lower/more ambiguous than
even the harshest synthetic condition (-12dB Gaussian SNR) produced. That
points at the NOISE MODEL, not the thresholding logic: white Gaussian
noise doesn't capture the structured, non-Gaussian interference real
mics actually pick up on this property (wind, other overlapping calls,
insects/cicadas, canopy reverb/multipath) — all of which suppress a
whitened correlation peak more than i.i.d. white noise at the same RMS.

This script builds a pool of REAL noise segments from the property's own
real pulled audio (test/tdoa_pulls/**, test/tdoa_real_batch/*) — for each
real WAV, finds its loudest ~300ms window (the call, if any) via a coarse
energy envelope and excludes it, keeping everything else in the clip as a
real-noise segment. Trials are built exactly as in
calibrate_phat_masked_coef.py (known injected delay on a clean Xeno-canto
call, target SNR set by scaling to a reference RMS) except the noise
added is a randomly drawn, RMS-rescaled excerpt from this real pool
instead of rng.normal(...) — same target SNR, real spectral/temporal
texture.

Report-only: does not modify server/correlation.py or
config/species_tdoa_params.json, does not touch the real WAVs it reads.

Usage:
    python tools/calibrate_phat_masked_coef_realnoise.py --species "Laughing Kookaburra"

Requires: numpy, scipy, soundfile, ffmpeg on PATH. Run from the sound-hub
root directory.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from math import gcd

import numpy as np
from scipy.ndimage import shift as nd_shift
from scipy.signal import resample_poly

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from derive_species_bands import (  # noqa: E402
    SAMPLES_DIR, dedupe_by_catalog, dedupe_by_content, load_all, load_current_config,
)
from validate_deramp_correlation import _find_calls  # noqa: E402
import correlation as prod_correlation  # noqa: E402
import onset_detection  # noqa: E402

from validate_toa_methods import (  # noqa: E402
    DEFAULT_GAIN_RANGE, DEFAULT_LAGS_MS, DEFAULT_SNR_DB, DEFAULT_SPECIES, _EXTRA_PAD_MS,
    _corr_phat_masked, _score_from_corr,
)
from calibrate_phat_masked_coef import (  # noqa: E402
    CANDIDATE_PERCENTILES, PROD_AMBIGUOUS_RATIO_THRESHOLD, PROD_MIN_PEAK_CORR_COEF, _percentile_report,
)

METHODS = ("raw", "phat_masked")

DEFAULT_NOISE_DIRS = [
    os.path.join(os.path.dirname(__file__), "..", "test", "tdoa_pulls"),
    os.path.join(os.path.dirname(__file__), "..", "test", "tdoa_real_batch"),
]

# How much of each real WAV's loudest window to exclude as "possibly the
# call itself" -- generous on purpose (better to discard some real noise
# than accidentally include call energy in the noise pool).
_EXCLUDE_HALF_MS = 150.0
_MIN_SEGMENT_MS = 50.0


def _build_noise_bank(noise_dirs: list, target_rate: int) -> list:
    segments = []
    n_files = 0
    for d in noise_dirs:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if not fn.lower().endswith(".wav"):
                    continue
                path = os.path.join(root, fn)
                try:
                    rate, data = onset_detection._load_mono(path)
                except Exception:
                    continue
                if len(data) < int(0.05 * rate):
                    continue
                if rate != target_rate:
                    g = gcd(rate, target_rate)
                    data = resample_poly(data, target_rate // g, rate // g)
                    rate = target_rate
                n_files += 1
                env = onset_detection._energy_envelope(data, rate, 10.0)
                peak_idx = int(np.argmax(env))
                excl = int(round(_EXCLUDE_HALF_MS * 1e-3 * rate))
                lo, hi = max(0, peak_idx - excl), min(len(data), peak_idx + excl)
                min_len = int(round(_MIN_SEGMENT_MS * 1e-3 * rate))
                before, after = data[:lo], data[hi:]
                if len(before) >= min_len:
                    segments.append(before)
                if len(after) >= min_len:
                    segments.append(after)
    total_s = sum(len(s) for s in segments) / target_rate if segments else 0.0
    print(f"Real noise bank: {len(segments)} segment(s) from {n_files} file(s), {total_s:.1f}s total")
    return segments


def _draw_real_noise(pool: list, length: int, rng: np.random.Generator) -> np.ndarray:
    out = np.zeros(0)
    guard = 0
    while len(out) < length and guard < 100:
        seg = pool[rng.integers(0, len(pool))]
        start = int(rng.integers(0, len(seg))) if len(seg) > 1 else 0
        piece = seg[start:]
        if len(piece) > 0:
            out = np.concatenate([out, piece])
        guard += 1
    if len(out) == 0:
        return np.zeros(length)
    if len(out) < length:
        out = np.tile(out, int(np.ceil(length / len(out))))
    start = int(rng.integers(0, len(out) - length + 1)) if len(out) > length else 0
    return out[start:start + length].copy()


def _run_one_trial(raw_data, rate, onset_idx, pre_samples, post_samples,
                    transit_samples, true_lag_samples, snr_db, gain, band_lo, band_hi,
                    noise_pool, rng):
    a_raw = raw_data[onset_idx - pre_samples: onset_idx + post_samples].copy()

    edge_pad = int(round(abs(true_lag_samples))) + 16
    local_lo = onset_idx - (pre_samples + transit_samples) - edge_pad
    local_hi = onset_idx + (post_samples + transit_samples) + edge_pad
    local_slice = raw_data[local_lo:local_hi]
    shifted_local = nd_shift(local_slice, shift=true_lag_samples, order=3, mode="nearest")
    b_span_lo = (onset_idx - (pre_samples + transit_samples)) - local_lo
    b_span_hi = (onset_idx + (post_samples + transit_samples)) - local_lo
    b_clean = shifted_local[b_span_lo:b_span_hi].copy()

    ref_rms = float(np.sqrt(np.mean(a_raw.astype(np.float64) ** 2))) or 1e-9
    noise_std = ref_rms / (10.0 ** (snr_db / 20.0))

    # Real noise excerpt, RMS-rescaled to hit the SAME target noise level a
    # Gaussian draw would have -- only the TEXTURE changes, not the nominal
    # SNR, so results stay comparable to the Gaussian-noise calibration.
    noise_excerpt = _draw_real_noise(noise_pool, len(b_clean), rng)
    noise_rms = float(np.sqrt(np.mean(noise_excerpt.astype(np.float64) ** 2))) or 1e-9
    noise_scaled = noise_excerpt * (noise_std / noise_rms)
    b_raw_noisy = b_clean * gain + noise_scaled

    transit_us_offset = transit_samples * 1e6 / rate

    have_band = band_lo is not None and band_hi is not None
    if have_band:
        a_filt = onset_detection.bandpass_filter(a_raw, rate, band_lo, band_hi)
        b_filt = onset_detection.bandpass_filter(b_raw_noisy, rate, band_lo, band_hi)
    else:
        a_filt = a_raw
        b_filt = b_raw_noisy

    raw_score = prod_correlation._score_correlation(rate, a_filt, b_filt, "plain")
    raw_score["lag_us"] -= transit_us_offset

    masked_corr, masked_ceiling = _corr_phat_masked(a_raw, b_raw_noisy, rate, band_lo, band_hi)
    masked_score = _score_from_corr(masked_corr, rate, a_raw, b_raw_noisy, max_possible_peak=masked_ceiling)
    masked_score["lag_us"] -= transit_us_offset

    return {"raw": raw_score, "phat_masked": masked_score}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples-dir", default=SAMPLES_DIR)
    parser.add_argument("--noise-dirs", default=",".join(DEFAULT_NOISE_DIRS))
    parser.add_argument("--species", default=",".join(DEFAULT_SPECIES))
    parser.add_argument("--n-trials", type=int, default=6)
    parser.add_argument("--lags-ms", default=",".join(str(v) for v in DEFAULT_LAGS_MS))
    parser.add_argument("--snr-db", default=",".join(str(v) for v in DEFAULT_SNR_DB))
    parser.add_argument("--gain-min", type=float, default=DEFAULT_GAIN_RANGE[0])
    parser.add_argument("--gain-max", type=float, default=DEFAULT_GAIN_RANGE[1])
    parser.add_argument("--transit-ms", type=float, default=20.0)
    parser.add_argument("--threshold-factor", type=float, default=6.0)
    parser.add_argument("--background-pct", type=float, default=20.0)
    parser.add_argument("--window-ms", type=float, default=20.0)
    parser.add_argument("--min-duration-ms", type=float, default=80.0)
    parser.add_argument("--merge-gap-ms", type=float, default=150.0)
    parser.add_argument("--pad-ms", type=float, default=30.0)
    parser.add_argument("--tolerance-us", type=float, default=200.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--tmp-dir", default=None)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    species_list = [s.strip() for s in args.species.split(",") if s.strip()]
    lags_ms = [float(v) for v in args.lags_ms.split(",") if v.strip() != ""]
    snr_db_list = [float(v) for v in args.snr_db.split(",") if v.strip() != ""]
    noise_dirs = [d.strip() for d in args.noise_dirs.split(",") if d.strip()]

    noise_pool = _build_noise_bank(noise_dirs, target_rate=48000)
    if not noise_pool:
        print("No real noise segments found -- aborting.")
        return

    current_config = load_current_config()
    seg_kwargs = dict(
        window_ms=args.window_ms, threshold_factor=args.threshold_factor,
        background_pct=args.background_pct, min_duration_ms=args.min_duration_ms,
        merge_gap_ms=args.merge_gap_ms, pad_ms=args.pad_ms,
    )
    rng = np.random.default_rng(args.seed)

    own_tmp = args.tmp_dir is None
    tmp_root = args.tmp_dir or tempfile.mkdtemp(prefix="calibrate_phat_masked_realnoise_")

    pooled = {m: {"coef": {True: [], False: []}, "ratio": {True: [], False: []}} for m in METHODS}

    try:
        for species in species_list:
            params = current_config.get(species)
            if not params:
                print(f"\n=== {species} === (no species_tdoa_params entry, skipping)")
                continue
            pre_ms, post_ms = params.get("window_margin_pre_ms"), params.get("window_margin_post_ms")
            band_lo, band_hi = params.get("freq_band_low_hz"), params.get("freq_band_high_hz")
            if pre_ms is None or post_ms is None:
                print(f"\n=== {species} === (no window_margin_pre/post_ms, skipping)")
                continue

            species_dir = os.path.join(args.samples_dir, species)
            if not os.path.isdir(species_dir):
                print(f"\n=== {species} === (no sample dir, skipping)")
                continue
            files = [os.path.join(species_dir, f) for f in sorted(os.listdir(species_dir))
                     if f.lower().endswith((".mp3", ".wav"))]
            if not files:
                print(f"\n=== {species} === (no sample files, skipping)")
                continue

            print(f"\n=== {species} (pre={pre_ms}ms post={post_ms}ms band=[{band_lo},{band_hi}]Hz) ===")
            kept, _ = dedupe_by_catalog(files)
            species_tmp = os.path.join(tmp_root, species.replace(" ", "_"))
            try:
                loaded = load_all(kept, species_tmp)
            except RuntimeError as e:
                print(f"  [FLAG] {e}")
                continue
            kept, _ = dedupe_by_content(loaded)

            pre_samples = post_samples = None
            calls_by_file = {}
            for path in kept:
                rate, raw_data, _dur = loaded[path]
                if pre_samples is None:
                    pre_samples = int(round(pre_ms * 1e-3 * rate))
                    post_samples = int(round(post_ms * 1e-3 * rate))
                detect_data = (onset_detection.bandpass_filter(raw_data, rate, band_lo, band_hi)
                               if band_lo is not None and band_hi is not None else raw_data)
                calls = _find_calls(rate, detect_data, seg_kwargs)
                calls_by_file[path] = (rate, raw_data, calls)

            if pre_samples is None or not any(c for _, _, c in calls_by_file.values()):
                print("  [FLAG] no calls found for this species, skipping")
                continue

            rate0 = next(iter(calls_by_file.values()))[0]
            transit_samples = int(round(args.transit_ms * 1e-3 * rate0))
            pad_samples = int(round(_EXTRA_PAD_MS * 1e-3 * rate0))
            max_lag_samples = int(round(max(abs(v) for v in lags_ms) * 1e-3 * rate0))

            n_calls_tested = 0
            for path, (rate, raw_data, calls) in calls_by_file.items():
                for call in calls:
                    onset_idx = call["onset_idx"]
                    need_back = pre_samples + transit_samples + max_lag_samples + pad_samples
                    need_fwd = post_samples + transit_samples + max_lag_samples + pad_samples
                    if (onset_idx - need_back < max(0, call["prev_end"])
                            or onset_idx + need_fwd > min(len(raw_data), call["next_start"])):
                        continue
                    n_calls_tested += 1
                    for lag_ms in lags_ms:
                        true_lag_samples = lag_ms * 1e-3 * rate
                        true_lag_us = lag_ms * 1000.0
                        for snr_db in snr_db_list:
                            for _ in range(args.n_trials):
                                gain = rng.uniform(args.gain_min, args.gain_max)
                                scores = _run_one_trial(
                                    raw_data, rate, onset_idx, pre_samples, post_samples,
                                    transit_samples, true_lag_samples, snr_db, gain, band_lo, band_hi,
                                    noise_pool, rng,
                                )
                                for m in METHODS:
                                    s = scores[m]
                                    correct = abs(s["lag_us"] - true_lag_us) <= args.tolerance_us
                                    pooled[m]["coef"][correct].append(s["peak_corr_coef"])
                                    pooled[m]["ratio"][correct].append(s["quality_ratio"])

            if n_calls_tested == 0:
                print(f"  [FLAG] no calls with enough clean room to test for {species}")
    finally:
        if own_tmp:
            shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n=== Pooled across {len(species_list)} species, REAL noise "
          f"(tolerance={args.tolerance_us:.0f}us) ===")
    report = {}
    for m in METHODS:
        n_correct = len(pooled[m]["coef"][True])
        n_incorrect = len(pooled[m]["coef"][False])
        print(f"\n  --- {m} (n_correct={n_correct}, n_incorrect={n_incorrect}) ---")
        coef_report = _percentile_report(
            "peak_corr_coef", np.array(pooled[m]["coef"][True]), np.array(pooled[m]["coef"][False]))
        ratio_report = _percentile_report(
            "quality_ratio ", np.array(pooled[m]["ratio"][True]), np.array(pooled[m]["ratio"][False]))
        report[m] = {"n_correct": n_correct, "n_incorrect": n_incorrect,
                      "peak_corr_coef_candidates": coef_report, "quality_ratio_candidates": ratio_report,
                      "raw_values": {"coef_correct": pooled[m]["coef"][True], "coef_incorrect": pooled[m]["coef"][False],
                                     "ratio_correct": pooled[m]["ratio"][True], "ratio_incorrect": pooled[m]["ratio"][False]}}

    print(f"\n  --- sanity check: production's EXISTING thresholds "
          f"(MIN_PEAK_CORR_COEF={PROD_MIN_PEAK_CORR_COEF}, AMBIGUOUS_RATIO_THRESHOLD={PROD_AMBIGUOUS_RATIO_THRESHOLD}) ---")
    for m in METHODS:
        correct_coef = np.array(pooled[m]["coef"][True])
        correct_ratio = np.array(pooled[m]["ratio"][True])
        incorrect_coef = np.array(pooled[m]["coef"][False])
        incorrect_ratio = np.array(pooled[m]["ratio"][False])
        if len(correct_coef) == 0:
            continue
        pass_correct = np.mean((correct_coef >= PROD_MIN_PEAK_CORR_COEF) & (correct_ratio >= PROD_AMBIGUOUS_RATIO_THRESHOLD))
        pass_incorrect = (np.mean((incorrect_coef >= PROD_MIN_PEAK_CORR_COEF) & (incorrect_ratio >= PROD_AMBIGUOUS_RATIO_THRESHOLD))
                           if len(incorrect_coef) else float("nan"))
        print(f"    {m:12s}: existing thresholds trust {pass_correct*100:5.1f}% of correct trials, "
              f"{pass_incorrect*100:5.1f}% of incorrect trials")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nWrote full report to {args.json_out}")

    print("\nSynthetic-trial-construction / REAL-noise-injection calibration -- does not modify "
          "server/correlation.py or config/species_tdoa_params.json.")


if __name__ == "__main__":
    main()
