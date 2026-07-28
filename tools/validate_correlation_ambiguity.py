"""
validate_correlation_ambiguity.py — synthetic ground-truth test of whether a
species' characterized bandpass band (narrow/low-frequency in particular)
makes leading-edge correlation more prone to AMBIGUOUS_RATIO_THRESHOLD
rejection, independent of window width or noise.

Why this exists (2026-07-27): live hub data post-deployment showed Pied
Currawong dominating uncorrelated/untrusted TDOA arrivals (527 of 569
untrusted rows, ~93% of the whole problem, despite having a SMALLER
correlation window than Torresian Crow and much smaller than the
__default__-fallback species — ruling out window width as the driver, see
project_soundhub_correlation_visibility memory's live-data follow-up).
Currawong's configured band ([670, 3360]Hz, 2690Hz bandwidth) is the
narrowest and lowest-frequency-ceiling of any configured species (Crow
5970Hz, Gray Butcherbird 5820Hz, Noisy Miner 8650Hz) — a narrower, lower
bandpass leaves a more tonal/periodic-looking waveform after filtering, and
a periodic signal is exactly what produces a strong competing correlation
peak at the "wrong" lag (a multiple of the underlying period), independent
of window width or SNR.

Methodology: same synthetic-known-delay construction as
validate_deramp_correlation.py (real Xeno-canto call, fractional-sample
delay via cubic spline, independent noise + random gain simulating a second
node's capture) — but this script holds SNR/gain/delay conditions fixed and
moderate (this isn't a noise-robustness question) and instead compares, for
each species, the SAME calls scored twice: once with the species' actual
configured bandpass band applied (matching production), and once
completely unfiltered (raw broadband) -- reporting peak_corr_coef/
quality_ratio/trusted-rate for both. If band-narrowness is really the
driver, a species' unfiltered condition should show a meaningfully better
trusted rate than its filtered condition, and that gap should be largest
for the narrowest-band species (Pied Currawong) and smallest/absent for
wide-band species.

Uses server/correlation.py's own _score_correlation and trust-threshold
constants directly (not reimplemented) — same "test the real function"
discipline as validate_deramp_correlation.py's regression-test fallout.

Report-only: does not modify server/correlation.py, config/
species_tdoa_params.json, or any production file.

Usage:
    python tools/validate_correlation_ambiguity.py
    python tools/validate_correlation_ambiguity.py --species "Pied Currawong,Torresian Crow"
    python tools/validate_correlation_ambiguity.py --n-trials 20 --json-out report.json

Requires: numpy, scipy, ffmpeg on PATH. Run from the sound-hub root
directory (same import convention as validate_deramp_correlation.py).
"""

import argparse
import json
import os
import shutil
import sys
import tempfile

import numpy as np
from scipy.ndimage import shift as nd_shift

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from derive_species_bands import (  # noqa: E402
    DEFAULT_EXCLUDE, SAMPLES_DIR, dedupe_by_catalog, dedupe_by_content,
    load_all, load_current_config,
)
from validate_deramp_correlation import _find_calls  # noqa: E402
import correlation as prod_correlation  # noqa: E402
import onset_detection  # noqa: E402

DEFAULT_SPECIES = [
    "Pied Currawong", "Torresian Crow", "Gray Butcherbird", "Noisy Miner",
]
DEFAULT_LAGS_MS = [-18.0, -9.0, 0.0, 9.0, 18.0]
DEFAULT_SNR_DB = 15.0  # a single, moderate/realistic condition -- this is
# a structural-ambiguity question, not a noise-robustness sweep (see
# validate_deramp_correlation.py for that).
DEFAULT_GAIN_RANGE = (0.5, 1.0)
DEFAULT_TRANSIT_MS = 20.0
_EXTRA_PAD_MS = 20.0


def _run_one_trial(
    data: np.ndarray, rate: int, onset_idx: int,
    pre_samples: int, post_samples: int, transit_samples: int,
    true_lag_samples: float, snr_db: float, gain: float,
    rng: np.random.Generator,
) -> dict:
    """Build one synthetic origin/neighbour pair (same construction as
    validate_deramp_correlation.py's _run_one_trial) and score it with
    today's production method — returns peak_corr_coef/quality_ratio/
    trusted, not lag error (this script cares about the trust gate, not
    lag accuracy)."""
    a = data[onset_idx - pre_samples: onset_idx + post_samples].copy()

    edge_pad = int(round(abs(true_lag_samples))) + 16
    local_lo = onset_idx - (pre_samples + transit_samples) - edge_pad
    local_hi = onset_idx + (post_samples + transit_samples) + edge_pad
    local_slice = data[local_lo:local_hi]
    shifted_local = nd_shift(local_slice, shift=true_lag_samples, order=3, mode="nearest")
    b_span_lo = (onset_idx - (pre_samples + transit_samples)) - local_lo
    b_span_hi = (onset_idx + (post_samples + transit_samples)) - local_lo
    b_clean = shifted_local[b_span_lo:b_span_hi].copy()

    ref_rms = float(np.sqrt(np.mean(a.astype(np.float64) ** 2))) or 1e-9
    noise_std = ref_rms / (10.0 ** (snr_db / 20.0))
    b_noisy = b_clean * gain + rng.normal(0.0, noise_std, size=b_clean.shape)

    score = prod_correlation._score_correlation(rate, a, b_noisy, "plain")
    trusted = (
        score["peak_corr_coef"] >= prod_correlation.MIN_PEAK_CORR_COEF
        and score["quality_ratio"] >= prod_correlation.AMBIGUOUS_RATIO_THRESHOLD
    )
    return {
        "peak_corr_coef": score["peak_corr_coef"],
        "quality_ratio": score["quality_ratio"],
        "trusted": trusted,
    }


def _summarize(results: list) -> dict:
    if not results:
        return {"n": 0}
    coefs = np.array([r["peak_corr_coef"] for r in results])
    ratios = np.array([min(r["quality_ratio"], 1e6) for r in results])
    trusted = np.array([r["trusted"] for r in results])
    return {
        "n": len(results),
        "trusted_frac": float(np.mean(trusted)),
        "avg_coef": float(np.mean(coefs)),
        "avg_ratio_bounded": float(np.mean(ratios)),
        "median_ratio_bounded": float(np.median(ratios)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples-dir", default=SAMPLES_DIR)
    parser.add_argument("--species", default=",".join(DEFAULT_SPECIES))
    parser.add_argument("--n-trials", type=int, default=12,
                         help="Repeated noise/gain realizations per (call, lag) combination")
    parser.add_argument("--lags-ms", default=",".join(str(v) for v in DEFAULT_LAGS_MS))
    parser.add_argument("--snr-db", type=float, default=DEFAULT_SNR_DB)
    parser.add_argument("--gain-min", type=float, default=DEFAULT_GAIN_RANGE[0])
    parser.add_argument("--gain-max", type=float, default=DEFAULT_GAIN_RANGE[1])
    parser.add_argument("--transit-ms", type=float, default=DEFAULT_TRANSIT_MS)
    parser.add_argument("--threshold-factor", type=float, default=6.0)
    parser.add_argument("--background-pct", type=float, default=20.0)
    parser.add_argument("--window-ms", type=float, default=20.0)
    parser.add_argument("--min-duration-ms", type=float, default=80.0)
    parser.add_argument("--merge-gap-ms", type=float, default=150.0)
    parser.add_argument("--pad-ms", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--tmp-dir", default=None)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    species_list = [s.strip() for s in args.species.split(",") if s.strip()]
    lags_ms = [float(v) for v in args.lags_ms.split(",") if v.strip() != ""]

    current_config = load_current_config()
    seg_kwargs = dict(
        window_ms=args.window_ms, threshold_factor=args.threshold_factor,
        background_pct=args.background_pct, min_duration_ms=args.min_duration_ms,
        merge_gap_ms=args.merge_gap_ms, pad_ms=args.pad_ms,
    )
    rng = np.random.default_rng(args.seed)

    own_tmp = args.tmp_dir is None
    tmp_root = args.tmp_dir or tempfile.mkdtemp(prefix="validate_ambiguity_")
    full_report = {}

    try:
        for species in species_list:
            params = current_config.get(species)
            if not params:
                print(f"\n=== {species} === (no species_tdoa_params entry, skipping)")
                continue
            pre_ms = params.get("window_margin_pre_ms")
            post_ms = params.get("window_margin_post_ms")
            band_lo = params.get("freq_band_low_hz")
            band_hi = params.get("freq_band_high_hz")
            if pre_ms is None or post_ms is None:
                print(f"\n=== {species} === (no window_margin_pre/post_ms, skipping)")
                continue
            bandwidth = (band_hi - band_lo) if (band_lo is not None and band_hi is not None) else None

            species_dir = os.path.join(args.samples_dir, species)
            if not os.path.isdir(species_dir):
                print(f"\n=== {species} === (no sample dir, skipping)")
                continue
            files = [os.path.join(species_dir, f) for f in sorted(os.listdir(species_dir))
                     if f.lower().endswith((".mp3", ".wav"))]
            if not files:
                print(f"\n=== {species} === (no sample files, skipping)")
                continue

            print(f"\n=== {species} (band=[{band_lo},{band_hi}]Hz bandwidth={bandwidth} "
                  f"pre={pre_ms}ms post={post_ms}ms) ===")
            kept, _ = dedupe_by_catalog(files)
            species_tmp = os.path.join(tmp_root, species.replace(" ", "_"))
            try:
                loaded = load_all(kept, species_tmp)
            except RuntimeError as e:
                print(f"  [FLAG] {e}")
                continue
            kept, _ = dedupe_by_content(loaded)

            results_filtered = []
            results_unfiltered = []
            n_calls_tested = 0

            for path in kept:
                rate, raw_data, _dur = loaded[path]
                if band_lo is not None and band_hi is not None:
                    filtered_data = onset_detection.bandpass_filter(raw_data, rate, band_lo, band_hi)
                else:
                    filtered_data = raw_data  # no band configured -- same as production's no-op

                pre_samples = int(round(pre_ms * 1e-3 * rate))
                post_samples = int(round(post_ms * 1e-3 * rate))
                transit_samples = int(round(args.transit_ms * 1e-3 * rate))
                pad_samples = int(round(_EXTRA_PAD_MS * 1e-3 * rate))
                max_lag_samples = int(round(max(abs(v) for v in lags_ms) * 1e-3 * rate))

                # Segment/onset-detect on the FILTERED data (matches
                # production, which always detects onset post-bandpass) --
                # the same call boundaries/onset indices are then reused
                # against the unfiltered array too, so both conditions
                # score literally the same underlying event.
                calls = _find_calls(rate, filtered_data, seg_kwargs)
                for call in calls:
                    onset_idx = call["onset_idx"]
                    need_back = pre_samples + transit_samples + max_lag_samples + pad_samples
                    need_fwd = post_samples + transit_samples + max_lag_samples + pad_samples
                    if (onset_idx - need_back < max(0, call["prev_end"])
                            or onset_idx + need_fwd > min(len(filtered_data), call["next_start"])):
                        continue

                    n_calls_tested += 1
                    for lag_ms in lags_ms:
                        true_lag_samples = lag_ms * 1e-3 * rate
                        for _ in range(args.n_trials):
                            gain = rng.uniform(args.gain_min, args.gain_max)
                            results_filtered.append(_run_one_trial(
                                filtered_data, rate, onset_idx, pre_samples, post_samples,
                                transit_samples, true_lag_samples, args.snr_db, gain, rng,
                            ))
                            results_unfiltered.append(_run_one_trial(
                                raw_data, rate, onset_idx, pre_samples, post_samples,
                                transit_samples, true_lag_samples, args.snr_db, gain, rng,
                            ))

            if n_calls_tested == 0:
                print(f"  [FLAG] no calls with enough clean room to test for {species}")
                continue

            sum_filtered = _summarize(results_filtered)
            sum_unfiltered = _summarize(results_unfiltered)
            print(f"  {n_calls_tested} call(s) tested, {sum_filtered['n']} trial(s) per condition")
            print(f"    filtered (production band):   trusted={sum_filtered['trusted_frac']*100:5.1f}%  "
                  f"avg_coef={sum_filtered['avg_coef']:.3f}  avg_ratio={sum_filtered['avg_ratio_bounded']:.3f}")
            print(f"    unfiltered (raw broadband):    trusted={sum_unfiltered['trusted_frac']*100:5.1f}%  "
                  f"avg_coef={sum_unfiltered['avg_coef']:.3f}  avg_ratio={sum_unfiltered['avg_ratio_bounded']:.3f}")
            gap = sum_unfiltered['trusted_frac'] - sum_filtered['trusted_frac']
            print(f"    unfiltered-minus-filtered trusted-rate gap: {gap*100:+.1f} percentage points "
                  f"(bandwidth={bandwidth}Hz)")

            full_report[species] = {
                "bandwidth_hz": bandwidth, "band_low_hz": band_lo, "band_high_hz": band_hi,
                "filtered": sum_filtered, "unfiltered": sum_unfiltered,
                "trusted_rate_gap": gap,
            }
    finally:
        if own_tmp:
            shutil.rmtree(tmp_root, ignore_errors=True)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(full_report, f, indent=2)
        print(f"\nWrote full report to {args.json_out}")

    print("\nSynthetic ground-truth validation only -- does not modify server/correlation.py "
          "or config/species_tdoa_params.json.")


if __name__ == "__main__":
    main()
