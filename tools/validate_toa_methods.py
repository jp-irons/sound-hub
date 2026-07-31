"""
validate_toa_methods.py — synthetic ground-truth comparison of candidate
time-of-arrival estimation methods against today's production leading-edge
correlation (server/correlation.py), using the same methodology as
validate_deramp_correlation.py/validate_correlation_ambiguity.py: a real
Xeno-canto call, a synthetic "neighbour" copy built via a KNOWN
fractional-sample delay (scipy.ndimage.shift, cubic spline) plus independent
noise and a random gain to simulate a second node's noisier/fainter capture,
scored against the known injected delay.

Why this exists (2026-07-29 discussion, following the TOA/correlation
investigation pivot — see project_soundhub_toa_investigation_pivot memory):
current production is leading-edge cross-correlation of raw amplitude
between the triggering (origin) node and each neighbour. This script tests
several alternatives against the same synthetic harness already validated
for the de-ramp and ambiguity investigations, plus the existing (currently
unused in production) gcc_phat method as a second baseline:

  raw               — today's production default (correlation_method="plain"
                      in species_tdoa_params.json): origin's own real-time
                      excerpt correlated directly against the neighbour.
  gcc_phat          — already implemented in correlation.py but not the
                      production default (tdoa-correlation-design-notes.md
                      found plain better once bandpass filtering is already
                      applied); included here as a second baseline, not a
                      new idea.
  matched_template  — instead of the origin's own (noisy) excerpt, correlate
                      a single clean reference call — held out from a
                      DIFFERENT file than the one being tested, so it can't
                      leak the answer — against the neighbour. Tests whether
                      a clean external template beats a live origin capture
                      that is itself subject to its own node's noise.
  onset_envelope    — correlate the short-time energy envelope of both
                      signals (clap_sync_check._energy_envelope) instead of
                      raw amplitude. Different from validate_deramp_
                      correlation.py's de-ramp idea (which DIVIDED OUT the
                      envelope) — this correlates the envelope shape itself.
                      Expected to help impulsive/transient calls and lose
                      fine timing precision on tonal ones.
  scot              — SCOT (SMoothed COherence Transform) weighting: cross-
                      spectrum normalized by sqrt(Saa*Sbb) instead of GCC-
                      PHAT's |Sab|. A coherence-style alternative to PHAT
                      that doesn't collapse every bin (including pure-noise
                      ones) to unit magnitude. Implemented locally in this
                      script (_corr_scot/_score_from_corr below), not added
                      to server/correlation.py.
  phat_masked       — Jon's 2026-07-29 proposed fix to gcc_phat's ordering:
                      whiten the RAW (unfiltered) cross-spectrum FIRST, then
                      apply the species' band as a tapered frequency-domain
                      mask on the already-whitened result, with nothing
                      after it that could renormalize the suppressed bins
                      back up. See _corr_phat_masked's docstring for the
                      algebra showing why today's bandpass-then-whiten order
                      cancels the filter's effect completely (not just
                      partially) at every bin with nonzero stopband gain,
                      and why masking has to happen strictly AFTER whitening
                      for the band restriction to survive to the IFFT.

This version builds each trial's origin/neighbour excerpts from RAW
(unfiltered) audio, then lets each method apply its own filtering (or none,
for phat_masked) at the point in the pipeline it structurally needs it —
an earlier version of this script pre-filtered each file once at load time
and injected synthetic noise afterward, unfiltered, which meant gcc_phat was
never actually exercising a real bandpass-then-whiten sequence on a noisy
composite signal the way production does on a live mic capture (the noise
component was never itself subject to any filtering pass at all). Onset
detection/segmentation still runs on a locally-filtered copy for finding
calls, matching production's own onset_detection_us convention, but that
filtered copy isn't retained or reused for scoring.

Uses server/correlation.py's own _score_correlation/_peak_quality/
_parabolic_peak directly for raw/gcc_phat/matched_template/onset_envelope
(same "test the real function" discipline as the other validate_*.py
scripts) — scot and phat_masked need a local scorer, since they need a
differently-weighted cross-spectrum that _score_correlation's method=
switch doesn't expose, and that local scorer still reuses correlation.py's
own _parabolic_peak/_peak_quality so the sign convention and quality-ratio
definition exactly match production.

Report-only: does not modify server/correlation.py or
config/species_tdoa_params.json.

Usage:
    python tools/validate_toa_methods.py
    python tools/validate_toa_methods.py --species "Pied Currawong,Torresian Crow"
    python tools/validate_toa_methods.py --n-trials 30 --json-out report.json

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

from clap_sync_check import _energy_envelope  # noqa: E402
from derive_species_bands import (  # noqa: E402
    SAMPLES_DIR, dedupe_by_catalog, dedupe_by_content, load_all,
    load_current_config,
)
from validate_deramp_correlation import _find_calls  # noqa: E402
import correlation as prod_correlation  # noqa: E402
import onset_detection  # noqa: E402

# Same fine envelope used everywhere else in this pipeline (onset_detection.py/
# derive_onset_margins.py/validate_deramp_correlation.py) — reused here for
# onset_envelope so this experiment isn't introducing a fifth, differently-
# tuned smoothing window into the mix.
_ONSET_WINDOW_MS = 3.0

METHODS = ("raw", "gcc_phat", "matched_template", "onset_envelope", "scot", "phat_masked")

# Same species subset as validate_deramp_correlation.py's DEFAULT_SPECIES —
# spans the real range of attack character on this property (Laughing
# Kookaburra's 36.5ms fast attack vs Sulphur-crested Cockatoo's 375ms slow
# one). Excludes Pheasant Coucal (already flagged elsewhere as a poor fit
# for this whole onset-detection pipeline).
DEFAULT_SPECIES = [
    "Laughing Kookaburra", "Gray Butcherbird", "Torresian Crow",
    "Sulphur-crested Cockatoo",
]

DEFAULT_LAGS_MS = [-18.0, -9.0, -3.0, 0.0, 3.0, 9.0, 18.0]
DEFAULT_SNR_DB = [20.0, 8.0]
DEFAULT_GAIN_RANGE = (0.3, 1.0)
# Extra room (beyond knee+transit) each candidate call needs on both sides
# to safely host the widest lag/transit combination being tested, same
# convention as validate_deramp_correlation.py.
_EXTRA_PAD_MS = 20.0


def _max_possible_peak(R: np.ndarray, n_fft: int) -> float:
    """Theoretical ceiling of |corr[n]| over every lag n, for a cross-
    correlation built by IFFT-ing the frequency-domain array R (found
    2026-07-30, fixing a real normalization bug — see _score_from_corr's
    docstring for the full story).

    Derivation: corr[n] = irfft(R, n_fft)[n] = (1/n_fft) * a weighted sum of
    R[k]*exp(i*phase(k,n)) terms. By the triangle inequality, |corr[n]| <=
    (1/n_fft) * sum_k |R[k]| * |exp(...)| = (1/n_fft) * sum_k |R[k]| for
    EVERY n — a bound that doesn't depend on n at all, so it's a ceiling on
    the whole correlation function, not just one point. That same
    upper-bound sum is exactly what you get by IFFT-ing |R| (a real,
    non-negative, phase-free array) and evaluating at n=0, where every
    phase term is 1: irfft(|R|, n_fft)[0]. Equality holds when every
    surviving bin's phase aligns at n — i.e. the perfect-match case — so
    this is the TIGHT ceiling, not a loose one.
    """
    ceiling = np.fft.irfft(np.abs(R), n_fft)
    return float(ceiling[0])


def _corr_scot(a: np.ndarray, b: np.ndarray, smooth_bins: int = 9):
    """SCOT-weighted cross-correlation, reassembled into the same
    [-(len(b)-1) .. +(len(a)-1)] layout scipy.signal.correlate(mode="full")
    and correlation.py's gcc_phat branch use, so downstream lag/quality math
    is identical either way. Returns (corr, max_possible_peak) — see
    _score_from_corr's docstring for why the second value now matters.

    IMPORTANT (found via this script's own smoke test, 2026-07-29): for a
    single-shot pair with NO smoothing, |Sab| = |A||B| = sqrt(Saa*Sbb) is an
    algebraic identity — R = Sab/|Sab| (PHAT) and R = Sab/sqrt(Saa*Sbb)
    (naive "SCOT") are then bit-for-bit the same normalization, not two
    different methods. The "Smoothed" in Smoothed COherence Transform is
    load-bearing: real SCOT smooths Saa/Sbb (a local moving average across
    neighbouring frequency bins, smooth_bins wide) BEFORE dividing, while
    leaving the numerator Sab unsmoothed so phase (and therefore the
    correlation peak's lag) isn't blurred. Once smoothed, smooth(Saa)*
    smooth(Sbb) genuinely differs from the unsmoothed Saa*Sbb (the average of
    a product isn't the product of averages), which is what makes this
    actually distinct from PHAT: a narrow spike of energy at one exact bin
    (more PHAT-like, keeps that bin's weight sharp) is treated differently
    from broadly-distributed energy across several neighbouring bins (SCOT
    smooths across it, approximating a local SNR-based weight rather than an
    instantaneous per-bin phase-only normalization) — this is exactly PHAT's
    known weakness at low SNR (tdoa-correlation-design-notes.md section 7's
    finding that PHAT re-amplifies a bandpass filter's residual stopband
    content, since every bin including near-silent ones gets normalized to
    unit magnitude)."""
    n = len(a) + len(b) - 1
    n_fft = 1
    while n_fft < n:
        n_fft *= 2
    A = np.fft.rfft(a, n_fft)
    B = np.fft.rfft(b, n_fft)
    Sab = A * np.conj(B)
    Saa = np.abs(A) ** 2
    Sbb = np.abs(B) ** 2

    kernel = np.ones(smooth_bins) / smooth_bins
    Saa_smooth = np.convolve(Saa, kernel, mode="same")
    Sbb_smooth = np.convolve(Sbb, kernel, mode="same")

    denom = np.sqrt(Saa_smooth * Sbb_smooth)
    denom[denom == 0] = 1e-12
    R = Sab / denom
    corr_full = np.fft.irfft(R, n_fft)
    corr = np.concatenate((corr_full[-(len(b) - 1):], corr_full[: len(a)]))
    return corr, _max_possible_peak(R, n_fft)


def _score_from_corr(
    corr: np.ndarray, rate: int, a: np.ndarray, b: np.ndarray,
    max_possible_peak: float | None = None,
    transit_s: float = 0.0,
) -> dict:
    """Same peak-picking/lag/quality logic as correlation.py's
    _score_correlation, applied to an externally computed corr array
    (scot/phat_masked/local-gcc_phat weighting) instead of
    scipy.signal.correlate/PHAT's own. Reuses correlation.py's own
    _parabolic_peak/_peak_quality so the sign convention and quality-ratio
    definition exactly match production — see that module's
    _score_correlation docstring for why the lag sign is negated relative
    to scipy's raw peak_idx - (len(b) - 1) convention. transit_s (added
    2026-07-30, for the real-node-positions pass) bounds BOTH which peak can
    be selected as primary and which peak counts as a competitor, mirroring
    _score_correlation's own bounded-search logic exactly (same
    _valid_lag_index_range helper) — defaults to 0.0 (unbounded), matching
    every caller that doesn't have real geometry to bound against.

    peak_corr_coef normalization (fixed 2026-07-30 — see project discussion
    on the real-pulls run): correlation.py's OWN peak_corr_coef formula
    (corr[peak_idx] / sqrt(a_energy*b_energy)) is only mathematically
    bounded to [-1,1] for the 'plain' method, where corr really is the raw-
    amplitude cross-correlation and that denominator is the Cauchy-Schwarz
    bound on it. For a whitened/masked frequency-domain corr (scot,
    phat_masked, gcc_phat), corr lives on a completely different scale, and
    dividing by the raw-amplitude energy produces coefficients with no real
    ceiling — confirmed empirically on real field data 2026-07-30:
    gcc_phat's own built-in coefficient came out in the hundreds to
    thousands (162, 4853, 8155...) on real attempts, trivially blowing past
    correlation.py's MIN_PEAK_CORR_COEF=0.3 gate regardless of whether the
    match was actually good — i.e. it would make gcc_phat/phat_masked look
    confidently trusted almost independent of match quality, the "more
    trusted, not more correct" risk flagged before any hub deployment was
    considered. Passing max_possible_peak (see _max_possible_peak) switches
    to a properly-bounded coefficient for these methods instead: corr[peak]
    divided by the theoretical ceiling that SAME whitened/masked R could
    ever reach, the same role sqrt(a_energy*b_energy) plays for 'plain' but
    correctly derived for a phase-normalized correlation. Falls back to the
    old raw-energy denominator when max_possible_peak isn't given, for any
    caller that hasn't been updated (should be unused in this codebase as
    of this fix, but keeps the function's other callers from erroring)."""
    if transit_s > 0.0:
        valid_lo, valid_hi = prod_correlation._valid_lag_index_range(len(b), transit_s, rate)
        bound_lo, bound_hi = max(0, valid_lo), min(len(corr), valid_hi + 1)
        if bound_hi > bound_lo:
            peak_idx = bound_lo + int(np.argmax(corr[bound_lo:bound_hi]))
        else:
            peak_idx = int(np.argmax(corr))
    else:
        peak_idx = int(np.argmax(corr))
    lag_samples = -(peak_idx - (len(b) - 1)) - prod_correlation._parabolic_peak(corr, peak_idx)
    lag_us = lag_samples * 1e6 / rate
    quality_ratio = prod_correlation._peak_quality(
        corr, peak_idx, rate, transit_s=transit_s, b_len=len(b))
    if max_possible_peak is not None:
        peak_corr_coef = float(corr[peak_idx] / max_possible_peak) if max_possible_peak > 0 else 0.0
    else:
        a_energy = float(np.sum(a.astype(np.float64) ** 2))
        b_energy = float(np.sum(b.astype(np.float64) ** 2))
        denom = np.sqrt(a_energy * b_energy)
        peak_corr_coef = float(corr[peak_idx] / denom) if denom > 0 else 0.0
    return {"lag_us": lag_us, "quality_ratio": quality_ratio, "peak_corr_coef": peak_corr_coef}


def _band_mask(freqs: np.ndarray, low_hz: float, high_hz: float, taper_hz: float) -> np.ndarray:
    """Raised-cosine (Hann-edge) taper: ~1 inside [low_hz, high_hz], smoothly
    rolling to ~0 over taper_hz on each side. A hard rectangular gate here
    would ring in the time domain (sinc sidelobes) and could smear the very
    correlation peak this mask exists to sharpen — the mask must suppress
    out-of-band bins without introducing a new artifact of its own, since
    (per _corr_phat_masked's docstring) it's the LAST magnitude-affecting
    step before the IFFT and nothing downstream will clean up ringing."""
    lo_lo, lo_hi = max(0.0, low_hz - taper_hz), low_hz
    hi_lo, hi_hi = high_hz, high_hz + taper_hz
    mask = np.zeros_like(freqs)
    mask[(freqs >= lo_hi) & (freqs <= hi_lo)] = 1.0
    up = (freqs >= lo_lo) & (freqs < lo_hi)
    mask[up] = 0.5 * (1 - np.cos(np.pi * (freqs[up] - lo_lo) / max(lo_hi - lo_lo, 1e-9)))
    down = (freqs > hi_lo) & (freqs <= hi_hi)
    mask[down] = 0.5 * (1 + np.cos(np.pi * (freqs[down] - hi_lo) / max(hi_hi - hi_lo, 1e-9)))
    return mask


def _corr_phat_masked(
    a: np.ndarray, b: np.ndarray, rate: int,
    low_hz: float | None, high_hz: float | None,
    taper_frac: float = 0.15, min_taper_hz: float = 100.0,
):
    """Jon's proposed reordering (2026-07-29 discussion): whiten the RAW,
    unfiltered cross-spectrum FIRST — identical math to correlation.py's
    gcc_phat branch, just fed unfiltered a/b — THEN apply the species' band
    restriction as a tapered frequency-domain mask directly on the
    already-whitened R(f), with nothing after it that could renormalize the
    suppressed bins back up. Returns (corr, max_possible_peak) — see
    _score_from_corr's docstring for why the second value now matters.

    Why this actually fixes the bug, not just reorders two lines: the
    production bandpass (onset_detection.bandpass_filter, sosfiltfilt) is
    zero-phase — it filters forward then backward, so its effective
    frequency response G(f) is real, non-negative, and identical for both
    channels (same species band applied to both nodes). Filtering BEFORE
    whitening gives a cross-spectrum R_filt(f) = G(f)^2 * A(f)*conj(B(f)).
    PHAT then normalizes by |R_filt(f)|: the G(f)^2 term cancels EXACTLY,
    top and bottom, at every bin where G(f) != 0 — which for a real IIR
    filter's finite stopband attenuation is every bin, not just the passband
    ones. So filter-then-whiten produces the IDENTICAL normalized phase as
    no filtering at all, at every bin with any nonzero gain; the filtering
    step contributes nothing to gcc_phat's result except at bins that
    genuinely underflow to exact floating-point zero (already special-cased
    in correlation.py's own gcc_phat branch via denom[denom==0]=1e-12).
    Masking AFTER whitening — with no further normalization step to undo it
    — is the only ordering where the band restriction's suppression
    actually survives to the correlation peak.

    Falls back to plain (unmasked) full-band PHAT when low_hz/high_hz is
    None, matching gcc_phat's own no-band no-op convention.
    """
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

    if low_hz is not None and high_hz is not None:
        freqs = np.fft.rfftfreq(n_fft, d=1.0 / rate)
        taper_hz = max(taper_frac * (high_hz - low_hz), min_taper_hz)
        R_white = R_white * _band_mask(freqs, low_hz, high_hz, taper_hz)

    corr_full = np.fft.irfft(R_white, n_fft)
    corr = np.concatenate((corr_full[-(len(b) - 1):], corr_full[: len(a)]))
    return corr, _max_possible_peak(R_white, n_fft)


def _build_template(calls_by_file: dict, pre_samples: int, post_samples: int):
    """Pick this species' matched-filter template: the first call (by sorted
    file path) with enough clean room for a pre_samples/post_samples excerpt.
    Deliberately NOT the same call used as a test trial's own origin excerpt
    — the caller skips this exact (path, onset_idx) pair when building
    trials, so the template can never be correlated against a neighbour
    synthesized from itself. Returns (template_array, (path, onset_idx)) or
    (None, None) if no call in the species has enough room."""
    for path in sorted(calls_by_file.keys()):
        rate, data, calls = calls_by_file[path]
        for call in calls:
            onset_idx = call["onset_idx"]
            if (onset_idx - pre_samples >= max(0, call["prev_end"])
                    and onset_idx + post_samples <= min(len(data), call["next_start"])):
                template = data[onset_idx - pre_samples: onset_idx + post_samples].copy()
                return template, (path, onset_idx)
    return None, None


def _run_one_trial(
    raw_data: np.ndarray, rate: int, onset_idx: int,
    pre_samples: int, post_samples: int, transit_samples: int,
    true_lag_samples: float, snr_db: float, gain: float,
    template_filt,
    band_lo,
    band_hi,
    rng: np.random.Generator,
) -> dict:
    """One synthetic trial: build the origin excerpt + a delayed, noised,
    gain-scaled "neighbour" search buffer from RAW (unfiltered) audio
    (identical shift/noise construction to validate_deramp_correlation.py's
    _run_one_trial, just built from raw_data instead of a pre-filtered
    whole-file copy — see this module's docstring for why that matters for
    gcc_phat/phat_masked specifically). Each method then applies its own
    filtering — or none, for phat_masked — at the point in the pipeline it
    structurally needs it, and all six methods are scored, returning each
    one's lag_us estimate (caller computes error vs ground truth).

    template_filt is passed in already bandpass-filtered (once per species,
    by the caller) since it's invariant across trials — recomputing it here
    would just be redundant work, not a different result.
    """
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
    b_raw_noisy = b_clean * gain + rng.normal(0.0, noise_std, size=b_clean.shape)

    # Widening b's PRE side by transit_samples shifts b's own local-index-0
    # earlier than a's by that amount, independent of any real delay — must
    # be subtracted back out of every method's raw lag_us, same correction
    # correlate_leading_edge itself applies (project_soundhub_correlation_
    # sign_bug memory).
    transit_us_offset = transit_samples * 1e6 / rate

    have_band = band_lo is not None and band_hi is not None
    if have_band:
        a_filt = onset_detection.bandpass_filter(a_raw, rate, band_lo, band_hi)
        b_filt = onset_detection.bandpass_filter(b_raw_noisy, rate, band_lo, band_hi)
    else:
        a_filt = a_raw
        b_filt = b_raw_noisy

    results = {}

    raw_score = prod_correlation._score_correlation(rate, a_filt, b_filt, "plain")
    results["raw"] = raw_score["lag_us"] - transit_us_offset

    # Today's actual production ordering: bandpass FIRST (a_filt/b_filt,
    # built above from the noisy composite, not a pre-filtered-then-noised
    # signal), THEN PHAT-whiten — this is the real bug the 2026-07-29
    # discussion is about, not an idealized version of it.
    phat_score = prod_correlation._score_correlation(rate, a_filt, b_filt, "gcc_phat")
    results["gcc_phat"] = phat_score["lag_us"] - transit_us_offset

    if template_filt is not None:
        template_score = prod_correlation._score_correlation(rate, template_filt, b_filt, "plain")
        results["matched_template"] = template_score["lag_us"] - transit_us_offset
    else:
        results["matched_template"] = None

    a_env = _energy_envelope(a_filt, rate, _ONSET_WINDOW_MS)
    b_env = _energy_envelope(b_filt, rate, _ONSET_WINDOW_MS)
    env_score = prod_correlation._score_correlation(rate, a_env, b_env, "plain")
    results["onset_envelope"] = env_score["lag_us"] - transit_us_offset

    scot_corr, scot_ceiling = _corr_scot(a_filt, b_filt)
    scot_score = _score_from_corr(scot_corr, rate, a_filt, b_filt, max_possible_peak=scot_ceiling)
    results["scot"] = scot_score["lag_us"] - transit_us_offset

    # Jon's proposed reordering (2026-07-29): whiten the RAW, unfiltered
    # signal FIRST, then apply the band restriction as a tapered mask on the
    # already-whitened cross-spectrum — see _corr_phat_masked's docstring.
    masked_corr, masked_ceiling = _corr_phat_masked(a_raw, b_raw_noisy, rate, band_lo, band_hi)
    masked_score = _score_from_corr(masked_corr, rate, a_raw, b_raw_noisy, max_possible_peak=masked_ceiling)
    results["phat_masked"] = masked_score["lag_us"] - transit_us_offset

    return results


def _summarize(errs: list, tolerance_us: float) -> dict:
    arr = np.array(errs, dtype=np.float64)
    return {
        "n": len(arr),
        "bias_us": float(np.mean(arr)),
        "mean_abs_error_us": float(np.mean(np.abs(arr))),
        "median_abs_error_us": float(np.median(np.abs(arr))),
        "std_us": float(np.std(arr)),
        "within_tolerance_frac": float(np.mean(np.abs(arr) <= tolerance_us)),
    }


def _print_summary(label: str, summary: dict, tolerance_us: float):
    print(f"    {label:18s}: bias={summary['bias_us']:+8.1f}us  "
          f"mean|err|={summary['mean_abs_error_us']:8.1f}us  "
          f"median|err|={summary['median_abs_error_us']:8.1f}us  "
          f"std={summary['std_us']:8.1f}us  "
          f"within {tolerance_us:.0f}us: {summary['within_tolerance_frac']*100:5.1f}%")


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
    tmp_root = args.tmp_dir or tempfile.mkdtemp(prefix="validate_toa_")
    full_report = {}
    pooled_errors = {m: [] for m in METHODS}

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
            kept, _ = dedupe_by_catalog(files)
            species_tmp = os.path.join(tmp_root, species.replace(" ", "_"))
            try:
                loaded = load_all(kept, species_tmp)
            except RuntimeError as e:
                print(f"  [FLAG] {e}")
                continue
            kept, _ = dedupe_by_content(loaded)

            pre_samples = None
            post_samples = None
            calls_by_file = {}
            for path in kept:
                rate, raw_data, _dur = loaded[path]
                if pre_samples is None:
                    pre_samples = int(round(pre_ms * 1e-3 * rate))
                    post_samples = int(round(post_ms * 1e-3 * rate))
                # Onset detection/segmentation still runs on a filtered copy
                # (matches production's own onset_detection_us convention —
                # bandpass before detect_onset) but that filtered copy is
                # transient: only raw_data is retained for later scoring, so
                # each method in _run_one_trial applies its own filtering
                # (or none, for phat_masked) rather than inheriting one
                # filtering pass baked in for every method alike.
                if band_lo is not None and band_hi is not None:
                    detect_data = onset_detection.bandpass_filter(raw_data, rate, band_lo, band_hi)
                else:
                    detect_data = raw_data
                calls = _find_calls(rate, detect_data, seg_kwargs)
                calls_by_file[path] = (rate, raw_data, calls)

            if pre_samples is None or not any(c for _, _, c in calls_by_file.values()):
                print("  [FLAG] no calls found for this species, skipping")
                continue

            rate = next(iter(calls_by_file.values()))[0]

            template_raw, template_key = _build_template(calls_by_file, pre_samples, post_samples)
            if template_raw is None:
                print("  [FLAG] no call had enough clean room to build a matched_template — "
                      "matched_template will be skipped for this species")
                template_filt = None
            else:
                print(f"  matched_template source: {os.path.basename(template_key[0])} "
                      f"@ sample {template_key[1]}")
                if band_lo is not None and band_hi is not None:
                    template_filt = onset_detection.bandpass_filter(template_raw, rate, band_lo, band_hi)
                else:
                    template_filt = template_raw

            transit_samples = int(round(args.transit_ms * 1e-3 * rate))
            pad_samples = int(round(_EXTRA_PAD_MS * 1e-3 * rate))
            max_lag_samples = int(round(max(abs(v) for v in lags_ms) * 1e-3 * rate))

            species_errors = {m: [] for m in METHODS}
            n_calls_tested = 0

            for path, (rate, raw_data, calls) in calls_by_file.items():
                for call in calls:
                    onset_idx = call["onset_idx"]
                    if template_key is not None and (path, onset_idx) == template_key:
                        continue  # never test the template against a neighbour synthesized from itself
                    need_back = pre_samples + transit_samples + max_lag_samples + pad_samples
                    need_fwd = post_samples + transit_samples + max_lag_samples + pad_samples
                    if (onset_idx - need_back < max(0, call["prev_end"])
                            or onset_idx + need_fwd > min(len(raw_data), call["next_start"])):
                        continue  # not enough clean room around this call — skip

                    n_calls_tested += 1
                    for lag_ms in lags_ms:
                        true_lag_samples = lag_ms * 1e-3 * rate
                        for snr_db in snr_db_list:
                            for _ in range(args.n_trials):
                                gain = rng.uniform(args.gain_min, args.gain_max)
                                lag_estimates = _run_one_trial(
                                    raw_data, rate, onset_idx, pre_samples, post_samples,
                                    transit_samples, true_lag_samples, snr_db, gain,
                                    template_filt, band_lo, band_hi, rng,
                                )
                                true_lag_us = lag_ms * 1000.0
                                for m in METHODS:
                                    est = lag_estimates[m]
                                    if est is None:
                                        continue
                                    err = est - true_lag_us
                                    species_errors[m].append(err)
                                    pooled_errors[m].append(err)

            if n_calls_tested == 0:
                print(f"  [FLAG] no calls with enough clean room to test for {species}")
                continue

            print(f"  {n_calls_tested} call(s) tested, "
                  f"{len(species_errors['raw'])} trial(s) total")
            species_summary = {}
            for m in METHODS:
                if not species_errors[m]:
                    print(f"    {m:18s}: skipped (no data)")
                    continue
                summary = _summarize(species_errors[m], args.tolerance_us)
                species_summary[m] = summary
                _print_summary(m, summary, args.tolerance_us)
            full_report[species] = species_summary
    finally:
        if own_tmp:
            shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n=== Pooled across {len(species_list)} species ===")
    pooled_summary = {}
    for m in METHODS:
        if not pooled_errors[m]:
            print(f"  {m}: no trials run")
            continue
        summary = _summarize(pooled_errors[m], args.tolerance_us)
        pooled_summary[m] = summary
        _print_summary(m, summary, args.tolerance_us)

    full_report["_pooled"] = pooled_summary
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(full_report, f, indent=2)
        print(f"\nWrote full report to {args.json_out}")

    print("\nSynthetic ground-truth validation only -- does not modify server/correlation.py "
          "or config/species_tdoa_params.json.")


if __name__ == "__main__":
    main()
