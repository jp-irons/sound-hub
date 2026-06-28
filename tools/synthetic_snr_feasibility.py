"""
synthetic_snr_feasibility.py — Feasibility test for bird-call TDOA at low SNR.

Why this exists:
    Hand-clap field tests (see clap_sync_check.py / run_clap_test.py) showed
    that even a sharp, deliberate, close-range clap only produces a strong,
    trustworthy cross-correlation about 1 time in 3 — the rest are weak
    (peak_corr_coef ~0.3) and noisy. Bird calls will generally be fainter,
    farther, and embedded in a continuous noise floor rather than a quiet
    gap, so the natural question is: how much further does the trustworthy
    fraction fall, and where exactly does it break down?

    This script answers that without needing weeks of wild bird detections.
    It takes a clean reference call recording, injects it into REAL
    recorded noise-floor segments from your own nodes at a precisely known
    (sub-sample) delay and a controlled signal-to-noise ratio, then runs the
    synthetic pair through the same onset-detection + leading-edge-trim +
    correlation pipeline used on real hardware. Because the true delay is
    known exactly, the estimation error is measurable directly — across many
    SNR levels, many trials per level, and (optionally) both plain
    cross-correlation and GCC-PHAT — producing an error-vs-SNR curve per
    method instead of a guess.

Mechanics:
    1. A clean call clip is delayed by an exact, possibly fractional, sample
       count using the FFT time-shift property (multiply the spectrum by a
       linear phase ramp, then inverse-FFT) — not a naive sample-rounded
       shift, which would defeat the purpose of testing sub-millisecond
       accuracy. The clip is padded with silence before delaying so the
       shift doesn't wrap a real part of the call around the buffer edge
       (numpy's FFT-based shift is inherently circular).
    2. The (un-delayed) clip is mixed into a random window of node A's noise
       recording at a target SNR (signal RMS / noise RMS, both measured
       over the call's own active duration, scaled to hit the requested
       value); the delayed copy is mixed into a random window of node B's
       noise recording at the same gain.
    3. detect_onset() runs on node A's synthetic buffer exactly as it would
       on real hardware — if it can't find a transient above its threshold,
       that's recorded as a miss (informative in its own right: it tells
       you the SNR floor below which the trigger itself stops firing,
       before correlation quality even becomes relevant).
    4. A window is sliced from both buffers around the detected onset,
       trimmed to the leading edge, and scored with plain cross-correlation
       (and GCC-PHAT, if requested). The estimated lag is compared against
       the known true delay to get a signed error in microseconds.
    5. Repeated many times per (delay, SNR) combination with fresh random
       noise draws, then summarized.

Usage:
    python tools/synthetic_snr_feasibility.py \\
        --call clean_bird_call.wav \\
        --noise-a quiet_node160.wav --noise-b quiet_node170.wav \\
        --snr-db-min -10 --snr-db-max 30 --snr-db-step 5 \\
        --true-delays-us -300,0,300 --trials 30 \\
        --out results.csv --plot results.png

Requires: numpy, scipy. matplotlib optional, only needed for --plot.
Run from the sound-hub root directory (so the clap_sync_check import works).
"""

import argparse
import csv
import sys

import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly, butter, sosfiltfilt

from clap_sync_check import (
    _load_mono, detect_onset, trim_to_leading_edge, _score_correlation,
)


def _resample_if_needed(rate, data, target_rate):
    if rate == target_rate:
        return data
    from math import gcd
    g = gcd(rate, target_rate)
    return resample_poly(data, target_rate // g, rate // g)


def fractional_delay(x: np.ndarray, tau_samples: float) -> np.ndarray:
    """Delay x by tau_samples (may be fractional) using the FFT time-shift
    property: shifting x(t) -> x(t - tau) corresponds to multiplying its
    spectrum by exp(-j*2*pi*f*tau). This is exact for any real tau, unlike
    rounding to the nearest sample, which is the whole point here -- we're
    deliberately testing sub-millisecond accuracy and can't afford a
    rounding error as large as the effect being measured.

    Caution: an FFT-based shift is inherently circular -- content shifted
    past one end wraps to the other. Callers should pad x with silence on
    both ends first (see `_pad` in main()) so any wraparound lands in dead
    silence rather than corrupting the call.
    """
    n = len(x)
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n)  # cycles per sample
    phase_ramp = np.exp(-1j * 2 * np.pi * freqs * tau_samples)
    shifted = np.fft.irfft(spectrum * phase_ramp, n=n)
    return shifted


def gcc_phat(rate: int, a: np.ndarray, b: np.ndarray, eps: float = 1e-12) -> dict:
    """GCC-PHAT scorer -- phase-only-normalized cross-correlation. See
    clap_sync_check.py's _score_correlation() for the plain-correlation
    equivalent and _peak_quality()/_parabolic_peak() for the shared
    peak-finding helpers this re-derives locally (kept self-contained here
    rather than importing private helpers from two places).

    PHAT's per-bin magnitude normalization sharpens the peak for a genuine
    broadband match but can equally sharpen a spurious peak built from pure
    noise when there's no real signal -- there is no energy-based trust
    metric equivalent to plain correlation's peak_corr_coef here, so
    quality_ratio (peak vs runner-up) is the only thing to lean on, and even
    that should be read with more suspicion than usual at low SNR.
    """
    n = len(a) + len(b)
    nfft = 1
    while nfft < n:
        nfft *= 2
    A = np.fft.rfft(a, n=nfft)
    B = np.fft.rfft(b, n=nfft)
    R = A * np.conj(B)
    R = R / (np.abs(R) + eps)
    cc = np.fft.irfft(R, n=nfft)

    max_lag = len(b) - 1
    cc_shifted = np.concatenate((cc[-max_lag:], cc[: len(a)]))
    peak_idx = int(np.argmax(cc_shifted))

    def parabolic(corr, idx):
        if idx <= 0 or idx >= len(corr) - 1:
            return 0.0
        y0, y1, y2 = corr[idx - 1], corr[idx], corr[idx + 1]
        denom = y0 - 2 * y1 + y2
        return 0.5 * (y0 - y2) / denom if denom != 0 else 0.0

    lag_samples = peak_idx - max_lag + parabolic(cc_shifted, peak_idx)
    lag_us = lag_samples * 1e6 / rate

    exclude = max(1, int(round(1.0e-3 * rate)))
    masked = cc_shifted.astype(np.float64).copy()
    lo, hi = max(0, peak_idx - exclude), min(len(masked), peak_idx + exclude + 1)
    masked[lo:hi] = -np.inf
    second_idx = int(np.argmax(masked))
    second_val = masked[second_idx]
    peak_val = cc_shifted[peak_idx]
    quality_ratio = float(peak_val / second_val) if (np.isfinite(second_val) and second_val > 0) else float("inf")

    return {"lag_us": lag_us, "quality_ratio": quality_ratio}


def derive_band(call: np.ndarray, rate: int, energy_lo_pct: float = 5.0,
                 energy_hi_pct: float = 95.0, margin_hz: float = 150.0) -> tuple:
    """Derive a (low_hz, high_hz) bandpass range from the call's own spectral
    energy distribution -- simulates "we know the species, so we know roughly
    where its call sits in frequency" (e.g. from a BirdNET-tagged detection).

    Uses the energy_lo_pct..energy_hi_pct cumulative-energy band (not min/max
    frequency with any energy at all, which would be dominated by spectral
    leakage and noise in the reference recording itself) plus a fixed margin
    on each side so the filter doesn't clip the edges of the call's own band.
    """
    n = len(call)
    spectrum = np.fft.rfft(call * np.hanning(n))
    mag2 = np.abs(spectrum) ** 2
    freqs = np.fft.rfftfreq(n, d=1.0 / rate)
    cum = np.cumsum(mag2)
    if cum[-1] <= 0:
        raise ValueError("call has zero energy -- cannot derive a band")
    cum /= cum[-1]
    lo_idx = int(np.searchsorted(cum, energy_lo_pct / 100.0))
    hi_idx = int(np.searchsorted(cum, energy_hi_pct / 100.0))
    low_hz = max(50.0, freqs[lo_idx] - margin_hz)
    high_hz = min(rate / 2.0 - 100.0, freqs[hi_idx] + margin_hz)
    if high_hz <= low_hz:
        raise ValueError(f"derived band is empty/inverted ({low_hz:.0f}-{high_hz:.0f} Hz) -- "
                          f"call may be too narrowband for this margin")
    return low_hz, high_hz


def bandpass_filter(x: np.ndarray, rate: int, low_hz: float, high_hz: float, order: int = 4) -> np.ndarray:
    """Zero-phase Butterworth bandpass (sosfiltfilt -- no group delay, so it
    doesn't itself bias the TDOA estimate). Intended to be applied to the
    full noisy buffer, simulating a node that knows (from BirdNET's species
    tag) roughly where to expect the call's energy and filters out everything
    else before onset detection and correlation.
    """
    nyq = rate / 2.0
    low = max(low_hz / nyq, 1e-4)
    high = min(high_hz / nyq, 0.999)
    sos = butter(order, [low, high], btype="band", output="sos")
    return sosfiltfilt(sos, x)


def compute_gain_for_snr(call: np.ndarray, noise_segment: np.ndarray, snr_db: float) -> float:
    """Scale factor to apply to `call` so that signal_rms / noise_rms hits
    the requested SNR (linear ratio = 10**(snr_db/20)). Both RMS values are
    measured over the full extent passed in -- callers should pass the
    call's active (non-padded) samples and a noise segment of comparable
    duration, not the whole buffer, so this reflects in-band SNR during the
    event rather than being diluted by silence either side of it.
    """
    signal_rms = float(np.sqrt(np.mean(call.astype(np.float64) ** 2)))
    noise_rms = float(np.sqrt(np.mean(noise_segment.astype(np.float64) ** 2)))
    if signal_rms <= 0 or noise_rms <= 0:
        raise ValueError("call or noise segment has zero RMS -- check inputs")
    target_linear = 10 ** (snr_db / 20.0)
    return target_linear * noise_rms / signal_rms


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--call", required=True, help="Clean reference call recording (WAV)")
    parser.add_argument("--noise-a", required=True, help="Quiet node-A recording, no real detections in it")
    parser.add_argument("--noise-b", help="Quiet node-B recording (defaults to --noise-a if omitted)")
    parser.add_argument("--rate", type=int, default=48000)
    parser.add_argument("--snr-db-min", type=float, default=-10.0)
    parser.add_argument("--snr-db-max", type=float, default=30.0)
    parser.add_argument("--snr-db-step", type=float, default=5.0)
    parser.add_argument("--true-delays-us", default="-300,0,300",
                         help="Comma-separated ground-truth delays to test, in microseconds")
    parser.add_argument("--trials", type=int, default=30,
                         help="Repeats per (delay, SNR) combination, each with a fresh random noise draw")
    parser.add_argument("--buffer-secs", type=float, default=1.0)
    parser.add_argument("--onset-window-secs", type=float, default=0.3,
                         help="Matches run_clap_test.py's default -- width of the window sliced "
                              "around the detected onset before leading-edge trimming")
    parser.add_argument("--leading-edge-pre-ms", type=float, default=1.0,
                         help="Override for trim_to_leading_edge()'s pre_ms -- the clap-tuned "
                              "default (1.0ms) is too narrow for bird calls with smooth/decaying "
                              "envelopes; widening this lets correlation see enough of the call "
                              "to lock a stable lag (see 2026-06-28 windowing investigation)")
    parser.add_argument("--leading-edge-post-ms", type=float, default=4.0,
                         help="Override for trim_to_leading_edge()'s post_ms -- see --leading-edge-pre-ms")
    parser.add_argument("--methods", default="plain,phat",
                         help="Comma-separated: plain, phat")
    parser.add_argument("--bandpass", action="store_true",
                         help="Apply a species-matched bandpass filter (band derived from --call's "
                              "own spectral energy distribution) to both noisy buffers before onset "
                              "detection and correlation -- simulates a node that knows the species "
                              "(from BirdNET's tag) and filters out-of-band noise before processing.")
    parser.add_argument("--bandpass-margin-hz", type=float, default=150.0,
                         help="Extra margin added outside the derived 5-95pct energy band, each side")
    parser.add_argument("--bandpass-order", type=int, default=4)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", default="synthetic_snr_results.csv")
    parser.add_argument("--plot", metavar="PNG_PATH", default=None)
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    rate = args.rate
    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    snr_levels = np.arange(args.snr_db_min, args.snr_db_max + 1e-9, args.snr_db_step)
    true_delays_us = [float(x) for x in args.true_delays_us.split(",")]

    call_rate, call = _load_mono(args.call)
    call = _resample_if_needed(call_rate, call, rate)
    # Pad with silence so fractional_delay()'s circular wraparound lands in
    # dead air, not in the call itself.
    pad = int(round(0.05 * rate))
    call_padded = np.concatenate([np.zeros(pad), call, np.zeros(pad)])

    band_low_hz = band_high_hz = None
    if args.bandpass:
        band_low_hz, band_high_hz = derive_band(call, rate, margin_hz=args.bandpass_margin_hz)
        print(f"[bandpass] derived band from --call spectrum: "
              f"{band_low_hz:.0f}-{band_high_hz:.0f} Hz (order {args.bandpass_order})", file=sys.stderr)

    noise_a_rate, noise_a = _load_mono(args.noise_a)
    noise_a = _resample_if_needed(noise_a_rate, noise_a, rate)
    if args.noise_b:
        noise_b_rate, noise_b = _load_mono(args.noise_b)
        noise_b = _resample_if_needed(noise_b_rate, noise_b, rate)
    else:
        noise_b = noise_a

    buffer_len = int(round(args.buffer_secs * rate))
    onset_pos = buffer_len // 2
    half_window = int(round(args.onset_window_secs / 2 * rate))

    if len(noise_a) < buffer_len or len(noise_b) < buffer_len:
        print(f"[ERROR] noise recordings must be at least {args.buffer_secs}s long", file=sys.stderr)
        sys.exit(1)
    if onset_pos + len(call_padded) > buffer_len:
        print(f"[ERROR] --buffer-secs too short for this call length + padding -- "
              f"need at least {(onset_pos + len(call_padded)) / rate:.2f}s", file=sys.stderr)
        sys.exit(1)

    rows = []
    total = len(true_delays_us) * len(snr_levels) * args.trials
    done = 0

    for true_delay_us in true_delays_us:
        tau_samples = true_delay_us * 1e-6 * rate
        delayed_call_padded = fractional_delay(call_padded, tau_samples)

        for snr_db in snr_levels:
            for trial in range(args.trials):
                a_start = rng.integers(0, len(noise_a) - buffer_len + 1)
                b_start = rng.integers(0, len(noise_b) - buffer_len + 1)
                buf_a = noise_a[a_start:a_start + buffer_len].copy()
                buf_b = noise_b[b_start:b_start + buffer_len].copy()

                lo, hi = onset_pos, onset_pos + len(call_padded)
                # Gain is calibrated from the noise actually present at the
                # injection site in THIS trial's draw, not a fixed slice taken
                # once per SNR level. Real outdoor noise is non-stationary --
                # using a fixed reference slice let the realized SNR drift
                # arbitrarily far from the requested target depending on
                # where that slice happened to land, which produced a stable,
                # SNR-independent bias instead of the expected error-vs-SNR
                # relationship (caught 2026-06-28 comparing estimated lag
                # against known injected delay -- estimated lag sat near zero
                # regardless of true delay at every SNR level).
                active_lo = lo + pad
                noise_ref = buf_a[active_lo:active_lo + len(call)]
                gain = compute_gain_for_snr(call, noise_ref, snr_db)

                buf_a[lo:hi] += gain * call_padded
                buf_b[lo:hi] += gain * delayed_call_padded

                if args.bandpass:
                    buf_a = bandpass_filter(buf_a, rate, band_low_hz, band_high_hz, args.bandpass_order)
                    buf_b = bandpass_filter(buf_b, rate, band_low_hz, band_high_hz, args.bandpass_order)

                row_base = dict(true_delay_us=true_delay_us, snr_db=snr_db, trial=trial,
                                 bandpass=args.bandpass)

                try:
                    onset_idx = detect_onset(buf_a, rate)
                except ValueError:
                    for method in methods:
                        rows.append({**row_base, "method": method, "onset_detected": False,
                                     "estimated_lag_us": np.nan, "error_us": np.nan,
                                     "quality_ratio": np.nan, "peak_corr_coef": np.nan})
                    done += 1
                    continue

                a_lo = max(0, onset_idx - half_window)
                a_hi = min(buffer_len, onset_idx + half_window)
                a_slice = buf_a[a_lo:a_hi]
                b_slice = buf_b[a_lo:a_hi]
                onset_in_slice = onset_idx - a_lo

                trimmed_a, trimmed_b = trim_to_leading_edge(
                    a_slice, b_slice, rate, onset_in_slice,
                    pre_ms=args.leading_edge_pre_ms, post_ms=args.leading_edge_post_ms)

                for method in methods:
                    if method == "plain":
                        score = _score_correlation(rate, trimmed_a, trimmed_b)
                        est_lag = score["lag_us"]
                        q = score["quality_ratio"]
                        coef = score["peak_corr_coef"]
                    elif method == "phat":
                        score = gcc_phat(rate, trimmed_a, trimmed_b)
                        est_lag = score["lag_us"]
                        q = score["quality_ratio"]
                        coef = np.nan
                    else:
                        raise ValueError(f"unknown method {method!r}")

                    rows.append({**row_base, "method": method, "onset_detected": True,
                                 "estimated_lag_us": est_lag, "error_us": est_lag - true_delay_us,
                                 "quality_ratio": q, "peak_corr_coef": coef})

                done += 1
                if done % 20 == 0 or done == total:
                    print(f"  {done}/{total} trials complete", file=sys.stderr)

    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["true_delay_us", "snr_db", "trial", "bandpass", "method",
                                                "onset_detected", "estimated_lag_us", "error_us",
                                                "quality_ratio", "peak_corr_coef"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows)} rows to {args.out}")

    print("\n--- Summary (mean |error|, miss rate) by SNR and method ---")
    for method in methods:
        print(f"\n  Method: {method}")
        for snr_db in snr_levels:
            subset = [r for r in rows if r["method"] == method and r["snr_db"] == snr_db]
            n = len(subset)
            misses = sum(1 for r in subset if not r["onset_detected"])
            errs = [abs(r["error_us"]) for r in subset if r["onset_detected"] and np.isfinite(r["error_us"])]
            if errs:
                mean_err = np.mean(errs)
                p90_err = np.percentile(errs, 90)
                print(f"    SNR {snr_db:+6.1f} dB:  miss rate {misses}/{n}  "
                      f"mean|err| {mean_err:7.1f}us  p90|err| {p90_err:7.1f}us")
            else:
                print(f"    SNR {snr_db:+6.1f} dB:  miss rate {misses}/{n}  (no successful detections)")

    if args.plot:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            print("[warn] matplotlib not installed -- skipping --plot", file=sys.stderr)
        else:
            fig, ax = plt.subplots(figsize=(8, 5))
            for method in methods:
                xs, ys = [], []
                for snr_db in snr_levels:
                    errs = [abs(r["error_us"]) for r in rows
                            if r["method"] == method and r["snr_db"] == snr_db
                            and r["onset_detected"] and np.isfinite(r["error_us"])]
                    if errs:
                        xs.append(snr_db)
                        ys.append(np.mean(errs))
                ax.plot(xs, ys, marker="o", label=method)
            ax.set_xlabel("SNR (dB)")
            ax.set_ylabel("Mean |TDOA error| (us)")
            ax.set_yscale("log")
            ax.legend()
            ax.set_title("TDOA estimation error vs SNR (synthetic injection test)")
            fig.tight_layout()
            fig.savefig(args.plot, dpi=150)
            print(f"Saved plot to {args.plot}")


if __name__ == "__main__":
    main()
