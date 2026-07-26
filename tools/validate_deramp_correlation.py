"""
validate_deramp_correlation.py — synthetic ground-truth test of whether
"de-ramping" (flattening the amplitude envelope) the leading-edge
correlation template/search window improves TDOA lag estimation accuracy
versus today's production method (server/correlation.py's raw-amplitude
correlate_leading_edge).

Why this exists (2026-07-26 design discussion): the knee-to-knee window
correlate_leading_edge() trims around an onset is, by construction, the
region where the call's energy envelope is rising from background up
through the detected transient — onset_detection.detect_onset fires at a
threshold-factor crossing early in that rise, not at the peak, so most of
the window on both sides is dominated by a shared rising trend rather than
flat signal. Cross-correlating two rising ramps is a known bias source:
any two monotonically-increasing sequences overlap well across a wide range
of lags almost regardless of their fine structure, which can smear or bias
the correlation peak away from the true µs-scale alignment this step exists
to measure precisely. Bandpass filtering (already applied upstream) does
not fix this — it restricts which carrier frequencies pass through, but an
amplitude envelope riding on top of the carrier survives bandpass filtering
intact.

This script cannot test the theory against real field data directly (we
never have ground truth for the true inter-node lag on a real bird call —
that's exactly the unknown quantity the pipeline is trying to estimate).
Instead it manufactures ground truth: take a real, clean Xeno-canto call,
create a synthetic "neighbour" copy via a KNOWN fractional-sample delay
(scipy.ndimage.shift, cubic spline — accurate for a signal already
bandpass-filtered well below Nyquist), add independent noise and a random
gain scale to simulate a second node's independent, noisier, possibly
fainter capture of the same physical event, then measure how far off each
scoring method's estimated lag is from the known injected delay.

Three scoring variants are compared, all reusing server/correlation.py's
own _score_correlation (the actual production scoring function — this
script does not reimplement or diverge from it):
  raw       — today's production method: correlate_leading_edge trims both
              buffers to a knee-sized/knee+transit-sized window and
              correlates the raw amplitude samples directly, unchanged.
  linear    — the proposed fix as literally described: divide each buffer
              by a piecewise-linear envelope model (background -> assumed
              peak -> background) built from its own smoothed short-time
              envelope. For the origin template this peak location is
              known exactly (it's the detected onset). For the neighbour's
              WIDER search buffer the true content's location is exactly
              what correlation is trying to find — this variant anchors
              the model on the same "naive center" production actually has
              available (origin_arrival_us translated via the neighbour's
              own t_start_us), which is the only honest way to implement
              this idea without assuming the answer.
  envelope  — a more general, location-agnostic alternative: divide each
              buffer, sample by sample, by its OWN local smoothed envelope
              (no assumed peak position needed at all) — flattens whatever
              the true rise/decay shape actually is rather than assuming a
              straight line, and needs no "where is the true content"
              assumption for the neighbour's search buffer either.

Report-only: does not modify server/correlation.py or any production file.

Usage:
    python tools/validate_deramp_correlation.py
    python tools/validate_deramp_correlation.py --species "Laughing Kookaburra,Sulphur-crested Cockatoo"
    python tools/validate_deramp_correlation.py --n-trials 30 --json-out report.json

Requires: numpy, scipy, ffmpeg on PATH. Run from the sound-hub root
directory (imports derive_species_bands.py/clap_sync_check.py from tools/,
and correlation.py/onset_detection.py from server/, same convention as
derive_onset_margins.py).
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

from clap_sync_check import _energy_envelope, _refine_to_steepest_rise  # noqa: E402
from derive_species_bands import (  # noqa: E402
    DEFAULT_EXCLUDE, SAMPLES_DIR, dedupe_by_catalog, dedupe_by_content,
    load_all, load_current_config, segment_calls,
)
import correlation as prod_correlation  # noqa: E402
import onset_detection  # noqa: E402

# Matches server/onset_detection.py's _ONSET_WINDOW_MS / tools/derive_onset_margins.py's
# _ONSET_WINDOW_MS — the same fine envelope used everywhere else in this
# pipeline to locate onsets/knees, reused here for the envelope-based
# de-ramp models too so this experiment isn't introducing a fourth,
# differently-tuned smoothing window into the mix.
_ONSET_WINDOW_MS = 3.0
_ONSET_REFINE_MARGIN_MS = 5.0

# How many species with an already-derived (non-flat-placeholder) knee value
# to test by default — deliberately spans the real range of attack
# character on this property (Laughing Kookaburra's 36.5ms fast attack vs
# Sulphur-crested Cockatoo's 375ms slow one) rather than assuming one
# species' result generalises. Excludes Pheasant Coucal (already flagged
# elsewhere as a poor fit for this whole onset-detection pipeline).
DEFAULT_SPECIES = [
    "Laughing Kookaburra", "Gray Butcherbird", "Torresian Crow",
    "Sulphur-crested Cockatoo",
]

DEFAULT_LAGS_MS = [-18.0, -9.0, -3.0, 0.0, 3.0, 9.0, 18.0]
DEFAULT_SNR_DB = [20.0, 8.0]
DEFAULT_GAIN_RANGE = (0.3, 1.0)
# Extra room (beyond knee+transit) each candidate call needs on both sides
# to safely host the widest lag/transit combination being tested, so the
# synthetic shift never runs off the edge of the recording or into a
# neighbouring call.
_EXTRA_PAD_MS = 20.0


def _deramp_envelope_norm(buf: np.ndarray, rate: int, floor_frac: float = 0.05) -> np.ndarray:
    """Location-agnostic de-ramp: divide every sample by its own local
    smoothed envelope. Needs no assumption about where the true content
    sits within buf — the only sound way to de-ramp the neighbour's WIDER
    search buffer, where that's exactly the unknown quantity."""
    env = _energy_envelope(buf, rate, _ONSET_WINDOW_MS)
    floor = max(floor_frac * float(np.median(env)), 1e-9)
    return buf / np.maximum(env, floor)


def _deramp_linear(buf: np.ndarray, rate: int, peak_idx_local: int,
                    floor_frac: float = 0.05) -> np.ndarray:
    """Jon's proposed fix, literally: a piecewise-linear envelope model
    (background -> peak_idx_local -> background), fit from buf's own
    smoothed envelope at its edges and at peak_idx_local, divided out.
    peak_idx_local is the ASSUMED peak location — exact for the origin
    template (its true onset), a best-guess "naive center" for the
    neighbour's search buffer (production has no better information at
    correlation time)."""
    env = _energy_envelope(buf, rate, _ONSET_WINDOW_MS)
    L = len(buf)
    peak_idx_local = int(np.clip(peak_idx_local, 0, L - 1))
    edge_n = max(1, L // 20)
    bg_pre = float(np.median(env[:edge_n]))
    bg_post = float(np.median(env[-edge_n:]))
    # Small local-max search around the assumed peak, not a single sample —
    # avoids the model being thrown off by one unlucky low sample right at
    # the assumed center.
    peak_lo = max(0, peak_idx_local - edge_n)
    peak_hi = min(L, peak_idx_local + edge_n + 1)
    peak_level = float(np.max(env[peak_lo:peak_hi])) if peak_hi > peak_lo else float(env[peak_idx_local])

    model = np.empty(L, dtype=np.float64)
    if peak_idx_local > 0:
        model[:peak_idx_local + 1] = np.linspace(bg_pre, peak_level, peak_idx_local + 1)
    else:
        model[0] = peak_level
    if L - 1 > peak_idx_local:
        model[peak_idx_local:] = np.linspace(peak_level, bg_post, L - peak_idx_local)

    floor = max(floor_frac * min(bg_pre, bg_post, peak_level if peak_level > 0 else bg_pre), 1e-9)
    return buf / np.maximum(model, floor)


def _find_calls(rate: int, data: np.ndarray, seg_kwargs: dict) -> list:
    """Segment a recording and refine each segment to its onset sample —
    same methodology as tools/derive_onset_margins.py's per-segment onset
    anchor, reused here rather than reimplemented."""
    segments = segment_calls(data=data, rate=rate, **seg_kwargs)
    fine_env = _energy_envelope(data, rate, _ONSET_WINDOW_MS)
    refine_margin = max(1, int(round(_ONSET_REFINE_MARGIN_MS * 1e-3 * rate)))
    calls = []
    for i, (start, end) in enumerate(segments):
        peak_idx = start + int(np.argmax(fine_env[start:end]))
        onset_idx = _refine_to_steepest_rise(data, peak_idx, refine_margin)
        prev_end = segments[i - 1][1] if i > 0 else 0
        next_start = segments[i + 1][0] if i < len(segments) - 1 else len(data)
        calls.append({
            "onset_idx": onset_idx, "prev_end": prev_end, "next_start": next_start,
        })
    return calls


def _run_one_trial(
    data: np.ndarray, rate: int, onset_idx: int,
    pre_samples: int, post_samples: int, transit_samples: int,
    true_lag_samples: float, snr_db: float, gain: float,
    rng: np.random.Generator,
) -> dict:
    """One synthetic trial: build the origin template + a delayed, noised,
    gain-scaled "neighbour" search buffer, score all three methods, return
    each method's raw lag_us estimate (caller computes error vs ground
    truth)."""
    a = data[onset_idx - pre_samples: onset_idx + post_samples].copy()

    # Shift only a local slice around onset_idx, not the whole (possibly
    # minutes-long) recording -- nd_shift's cubic-spline interpolation cost
    # scales with array length, and this runs once per trial. A few extra
    # samples of edge padding is enough since we only ever read back the
    # small b_span window from the middle of this local slice.
    edge_pad = int(round(abs(true_lag_samples))) + 16
    local_lo = onset_idx - (pre_samples + transit_samples) - edge_pad
    local_hi = onset_idx + (post_samples + transit_samples) + edge_pad
    local_slice = data[local_lo:local_hi]
    shifted_local = nd_shift(local_slice, shift=true_lag_samples, order=3, mode="nearest")
    b_span_lo = (onset_idx - (pre_samples + transit_samples)) - local_lo
    b_span_hi = (onset_idx + (post_samples + transit_samples)) - local_lo
    b_clean = shifted_local[b_span_lo:b_span_hi].copy()

    # Noise level fixed from the UNGAINED reference signal, then gain
    # applied — so a lower gain genuinely degrades effective SNR (a
    # farther/quieter node has a similar absolute noise floor but a weaker
    # desired signal), matching the real loudness-asymmetry motivation
    # behind cross-correlation in the first place.
    ref_rms = float(np.sqrt(np.mean(a.astype(np.float64) ** 2))) or 1e-9
    noise_std = ref_rms / (10.0 ** (snr_db / 20.0))
    b_noisy = b_clean * gain + rng.normal(0.0, noise_std, size=b_clean.shape)

    naive_center_local = pre_samples + transit_samples  # b's "onset" best-guess index

    # _score_correlation is called directly here (not via
    # correlate_leading_edge), so this script must apply the same
    # transit-offset correction correlate_leading_edge itself applies:
    # widening b's PRE side by transit_samples shifts b's own local-index-0
    # earlier than a's by that amount, independent of any real delay, and
    # leaks straight into the raw lag_us unless subtracted back out (see
    # project_soundhub_correlation_sign_bug memory — found and fixed
    # 2026-07-26 while building this exact script).
    transit_us_offset = transit_samples * 1e6 / rate

    results = {}

    raw_score = prod_correlation._score_correlation(rate, a, b_noisy, "plain")
    results["raw"] = raw_score["lag_us"] - transit_us_offset

    a_env = _deramp_envelope_norm(a, rate)
    b_env = _deramp_envelope_norm(b_noisy, rate)
    env_score = prod_correlation._score_correlation(rate, a_env, b_env, "plain")
    results["envelope"] = env_score["lag_us"] - transit_us_offset

    a_lin = _deramp_linear(a, rate, pre_samples)
    b_lin = _deramp_linear(b_noisy, rate, naive_center_local)
    lin_score = prod_correlation._score_correlation(rate, a_lin, b_lin, "plain")
    results["linear"] = lin_score["lag_us"] - transit_us_offset

    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples-dir", default=SAMPLES_DIR)
    parser.add_argument("--species", default=",".join(DEFAULT_SPECIES),
                         help="Comma-separated species subset")
    parser.add_argument("--n-trials", type=int, default=15,
                         help="Repeated noise/gain realizations per (call, lag, SNR) combination")
    parser.add_argument("--lags-ms", default=",".join(str(v) for v in DEFAULT_LAGS_MS))
    parser.add_argument("--snr-db", default=",".join(str(v) for v in DEFAULT_SNR_DB))
    parser.add_argument("--gain-min", type=float, default=DEFAULT_GAIN_RANGE[0])
    parser.add_argument("--gain-max", type=float, default=DEFAULT_GAIN_RANGE[1])
    parser.add_argument("--transit-ms", type=float, default=20.0,
                         help="Neighbour search-buffer transit widening, matching this "
                              "property's real worst-case NodeTransit")
    parser.add_argument("--threshold-factor", type=float, default=6.0)
    parser.add_argument("--background-pct", type=float, default=20.0)
    parser.add_argument("--window-ms", type=float, default=20.0)
    parser.add_argument("--min-duration-ms", type=float, default=80.0)
    parser.add_argument("--merge-gap-ms", type=float, default=150.0)
    parser.add_argument("--pad-ms", type=float, default=30.0)
    parser.add_argument("--tolerance-us", type=float, default=200.0,
                         help="Error tolerance (us) for the 'within tolerance' hit-rate metric")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--tmp-dir", default=None)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    species_list = [s.strip() for s in args.species.split(",") if s.strip()]
    lags_ms = [float(v) for v in args.lags_ms.split(",") if v.strip() != ""]
    snr_db_list = [float(v) for v in args.snr_db.split(",") if v.strip() != ""]

    current_config = load_current_config()
    seg_kwargs = dict(
        window_ms=args.window_ms, threshold_factor=args.threshold_factor,
        background_pct=args.background_pct, min_duration_ms=args.min_duration_ms,
        merge_gap_ms=args.merge_gap_ms, pad_ms=args.pad_ms,
    )
    rng = np.random.default_rng(args.seed)

    own_tmp = args.tmp_dir is None
    tmp_root = args.tmp_dir or tempfile.mkdtemp(prefix="validate_deramp_")
    methods = ("raw", "envelope", "linear")
    full_report = {}
    pooled_errors = {m: [] for m in methods}

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
            kept, dedupe_log = dedupe_by_catalog(files)
            species_tmp = os.path.join(tmp_root, species.replace(" ", "_"))
            try:
                loaded = load_all(kept, species_tmp)
            except RuntimeError as e:
                print(f"  [FLAG] {e}")
                continue
            kept, _ = dedupe_by_content(loaded)

            species_errors = {m: [] for m in methods}
            n_calls_tested = 0

            for path in kept:
                rate, data, _dur = loaded[path]
                if band_lo is not None and band_hi is not None:
                    data = onset_detection.bandpass_filter(data, rate, band_lo, band_hi)

                pre_samples = int(round(pre_ms * 1e-3 * rate))
                post_samples = int(round(post_ms * 1e-3 * rate))
                transit_samples = int(round(args.transit_ms * 1e-3 * rate))
                pad_samples = int(round(_EXTRA_PAD_MS * 1e-3 * rate))
                max_lag_samples = int(round(max(abs(v) for v in lags_ms) * 1e-3 * rate))

                calls = _find_calls(rate, data, seg_kwargs)
                for call in calls:
                    onset_idx = call["onset_idx"]
                    need_back = pre_samples + transit_samples + max_lag_samples + pad_samples
                    need_fwd = post_samples + transit_samples + max_lag_samples + pad_samples
                    if (onset_idx - need_back < max(0, call["prev_end"])
                            or onset_idx + need_fwd > min(len(data), call["next_start"])):
                        continue  # not enough clean room around this call — skip

                    n_calls_tested += 1
                    for lag_ms in lags_ms:
                        true_lag_samples = lag_ms * 1e-3 * rate
                        for snr_db in snr_db_list:
                            for _ in range(args.n_trials):
                                gain = rng.uniform(args.gain_min, args.gain_max)
                                lag_estimates = _run_one_trial(
                                    data, rate, onset_idx, pre_samples, post_samples,
                                    transit_samples, true_lag_samples, snr_db, gain, rng,
                                )
                                true_lag_us = lag_ms * 1000.0
                                for m in methods:
                                    err = lag_estimates[m] - true_lag_us
                                    species_errors[m].append(err)
                                    pooled_errors[m].append(err)

            if n_calls_tested == 0:
                print(f"  [FLAG] no calls with enough clean room to test for {species}")
                continue

            print(f"  {n_calls_tested} call(s) tested, {len(species_errors['raw'])} trial(s) total")
            species_summary = {}
            for m in methods:
                errs = np.array(species_errors[m])
                summary = {
                    "n": len(errs),
                    "bias_us": float(np.mean(errs)),
                    "mean_abs_error_us": float(np.mean(np.abs(errs))),
                    "median_abs_error_us": float(np.median(np.abs(errs))),
                    "std_us": float(np.std(errs)),
                    "within_tolerance_frac": float(np.mean(np.abs(errs) <= args.tolerance_us)),
                }
                species_summary[m] = summary
                print(f"    {m:10s}: bias={summary['bias_us']:+8.1f}us  "
                      f"mean|err|={summary['mean_abs_error_us']:8.1f}us  "
                      f"median|err|={summary['median_abs_error_us']:8.1f}us  "
                      f"std={summary['std_us']:8.1f}us  "
                      f"within {args.tolerance_us:.0f}us: {summary['within_tolerance_frac']*100:5.1f}%")
            full_report[species] = species_summary
    finally:
        if own_tmp:
            shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n=== Pooled across {len(species_list)} species ===")
    pooled_summary = {}
    for m in methods:
        errs = np.array(pooled_errors[m])
        if len(errs) == 0:
            print(f"  {m}: no trials run")
            continue
        summary = {
            "n": len(errs),
            "bias_us": float(np.mean(errs)),
            "mean_abs_error_us": float(np.mean(np.abs(errs))),
            "median_abs_error_us": float(np.median(np.abs(errs))),
            "std_us": float(np.std(errs)),
            "within_tolerance_frac": float(np.mean(np.abs(errs) <= args.tolerance_us)),
        }
        pooled_summary[m] = summary
        print(f"  {m:10s}: bias={summary['bias_us']:+8.1f}us  "
              f"mean|err|={summary['mean_abs_error_us']:8.1f}us  "
              f"median|err|={summary['median_abs_error_us']:8.1f}us  "
              f"std={summary['std_us']:8.1f}us  "
              f"within {args.tolerance_us:.0f}us: {summary['within_tolerance_frac']*100:5.1f}%")

    full_report["_pooled"] = pooled_summary
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(full_report, f, indent=2)
        print(f"\nWrote full report to {args.json_out}")

    print("\nSynthetic ground-truth validation only -- does not modify server/correlation.py.")


if __name__ == "__main__":
    main()
