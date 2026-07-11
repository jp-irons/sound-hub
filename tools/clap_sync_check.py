"""
clap_sync_check.py — Measure inter-node clock sync error from a shared clap.

Background:
    Both WAVs are pulled from the nodes using the SAME [tStartUs, tEndUs]
    window (see test/test-audio-pull.ps1 — that's the normal way to use the
    audio pull pipeline for this test). Since each node timestamps its own
    audio from its own GpsClock-derived epoch, both files are nominally
    anchored to the same absolute tStart. Any waveform-aligned lag found
    between the two recordings of the same clap *is* the inter-node sync
    error — no separate epoch bookkeeping is needed as long as both pulls
    used the same window.

    If the two pulls did NOT use the same tStart (different windows, or one
    request was a few seconds late), pass --offset-us to account for the
    known difference; the reported sync error is corrected for it.

Usage:
    python tools/clap_sync_check.py audio/audio_123_<macA>.wav audio/audio_124_<macB>.wav

    # If the two pulls used different tStartUs windows:
    python tools/clap_sync_check.py fileA.wav fileB.wav --offset-us 1500000

    # Save an alignment plot (requires matplotlib):
    python tools/clap_sync_check.py fileA.wav fileB.wav --plot out.png

Requires: numpy, scipy (see tools/requirements.txt). matplotlib optional,
only needed for --plot.

Run from the sound-hub root directory.
"""

import argparse
import sys

import numpy as np
from scipy.io import wavfile
from scipy.signal import correlate, resample_poly


def _load_mono(path: str):
    rate, data = wavfile.read(path)
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = data.astype(np.float64)
    data -= data.mean()
    return rate, data


def _parabolic_peak(corr: np.ndarray, peak_idx: int) -> float:
    """Sub-sample peak refinement via parabolic interpolation."""
    if peak_idx <= 0 or peak_idx >= len(corr) - 1:
        return 0.0
    y0, y1, y2 = corr[peak_idx - 1], corr[peak_idx], corr[peak_idx + 1]
    denom = (y0 - 2 * y1 + y2)
    if denom == 0:
        return 0.0
    return 0.5 * (y0 - y2) / denom


# How close (in samples) a secondary peak has to be to the primary one to be
# considered "the same event" (measurement jitter) rather than a competing
# event. 1 ms is generously larger than any expected true sync error or
# sub-sample refinement, but much smaller than typical room-echo delays.
_PEAK_EXCLUSION_MS = 1.0

# Below this primary/secondary ratio, the correlation is ambiguous enough
# that something other than the intended transient (echo, ambient noise over
# the multi-second window) may have won instead of the clap.
_AMBIGUOUS_RATIO_THRESHOLD = 1.5

# Below this normalized peak-correlation coefficient (peak / sqrt(energy_a *
# energy_b), capped at 1.0 for a perfect match), the correlation peak is too
# weak relative to the slices' own energy to trust — regardless of how high
# quality_ratio looks. Found necessary 2026-06-27 after trim_to_leading_edge()
# made quality_ratio unreliable on its own: a candidate slice with no real
# shared transient (e.g. a quiet, unrelated noise blip picked up by
# detect_onset_candidates()) has so little energy and structure in a ~5ms
# window that no competing second peak exists either — quality_ratio comes
# out as "inf" (nothing to divide by) even though the "match" is meaningless
# noise-on-noise alignment. This caught exactly that case in practice: a real
# clap scored 1.33x (finite, correctly flagged as marginal) while a spurious
# candidate scored "inf" and would otherwise have won outright.
_MIN_PEAK_CORR_COEF = 0.3


def _peak_quality(corr: np.ndarray, peak_idx: int, rate: int) -> tuple:
    """Ratio of the primary correlation peak to the next-highest peak found
    outside a small exclusion zone around it. A low ratio means some other
    correlated event in the window (echo, ambient noise) is nearly as strong
    as the primary peak — the lag derived from argmax() alone is then
    unreliable, since across runs whichever one is taller can flip with the
    noise floor. Returns (ratio, second_idx); ratio is +inf if no usable
    secondary peak exists.
    """
    exclude = max(1, int(round(_PEAK_EXCLUSION_MS * 1e-3 * rate)))
    masked = corr.astype(np.float64).copy()
    lo = max(0, peak_idx - exclude)
    hi = min(len(corr), peak_idx + exclude + 1)
    masked[lo:hi] = -np.inf
    second_idx = int(np.argmax(masked))
    second_val = masked[second_idx]
    peak_val = corr[peak_idx]
    if not np.isfinite(second_val) or second_val <= 0:
        return float("inf"), second_idx
    return float(peak_val / second_val), second_idx


# Onset detector — short-time energy envelope, refined to the steepest-rise
# sample. Good enough to find a sharp clap transient; this stands in for what
# a node would eventually do on-device to flag a segment worth sending to
# the hub (see CLAUDE.md's open question on node-side trigger criteria).
_ONSET_WINDOW_MS = 3.0          # short-time energy averaging window
_ONSET_THRESHOLD_FACTOR = 8.0   # multiple of background RMS the peak must exceed
_ONSET_REFINE_MARGIN_MS = 5.0   # +/- search margin around the peak for steepest-rise refinement

# How far apart (in time) two energy peaks in detect_onset_candidates() must
# be to count as separate events rather than the same transient's smear.
_CANDIDATE_MIN_SEPARATION_MS = 50.0


def _energy_envelope(data: np.ndarray, rate: int, window_ms: float) -> np.ndarray:
    win = max(1, int(round(window_ms * 1e-3 * rate)))
    energy = np.convolve(data.astype(np.float64) ** 2, np.ones(win) / win, mode="same")
    return np.sqrt(energy)


def _refine_to_steepest_rise(data: np.ndarray, idx: int, margin_samples: int) -> int:
    """Refine a coarse energy-envelope peak index to the steepest-rise raw
    sample within margin_samples of it. The smoothed envelope's peak
    lags/leads the true attack edge by up to roughly the smoothing window's
    width; this finds the actual edge rather than the smeared peak.
    """
    lo = max(0, idx - margin_samples)
    hi = min(len(data), idx + margin_samples)
    if hi - lo <= 1:
        return idx
    segment = np.abs(data[lo:hi].astype(np.float64))
    return lo + int(np.argmax(np.diff(segment)))


def detect_onset(data: np.ndarray, rate: int,
                  window_ms: float = _ONSET_WINDOW_MS,
                  threshold_factor: float = _ONSET_THRESHOLD_FACTOR,
                  margin_ms: float = _ONSET_REFINE_MARGIN_MS) -> int:
    """Return the sample index of the most prominent short-time-energy
    transient in `data`, refined to the steepest-rise sample within
    margin_ms of it.

    Takes the GLOBAL peak of the energy envelope, not the first sample that
    crosses threshold_factor x background RMS. A real outdoor buffer often
    has other transients ahead of the one you actually want (wind, insects,
    handling noise, an earlier clap from setup) — grabbing the first one
    above threshold can lock onto the wrong event entirely while still
    "succeeding" silently. threshold_factor is now just a sanity check: if
    even the loudest point in the buffer doesn't look like a real transient,
    raise rather than return a meaningless index.

    Caution: this single-best-guess picker has no defense against a
    competing transient (echo, wind, insects) that happens to be louder
    than the real event of interest — confirmed in practice to occasionally
    mis-pick. See detect_onset_candidates() for the cross-checked version
    used by run_clap_test.py.

    Error message precision bumped 2026-07-11 (kept in sync with
    sound-hub/server/onset_detection.py, the production port of this
    function — see its module docstring) after "background=0.0, peak=0.0"
    from the old 1-decimal format looked like a detector bug when it was
    actually just a real near-silent buffer with both values below 0.05.
    """
    rms = _energy_envelope(data, rate, window_ms)
    background = np.median(rms)
    threshold = max(background * threshold_factor, 1e-6)

    peak_idx = int(np.argmax(rms))
    if rms[peak_idx] <= threshold:
        duration_s = len(data) / rate if rate else 0.0
        peak_sample = float(np.max(np.abs(data))) if len(data) else 0.0
        raise ValueError(
            f"no transient found above {threshold_factor}x background RMS "
            f"(background={background:.5f}, peak_envelope={rms[peak_idx]:.5f}, "
            f"peak_sample={peak_sample:.5f}, duration={duration_s:.2f}s, "
            f"n_samples={len(data)})"
        )

    margin = max(1, int(round(margin_ms * 1e-3 * rate)))
    return _refine_to_steepest_rise(data, peak_idx, margin)


def detect_onset_candidates(data: np.ndarray, rate: int, k: int = 3,
                             min_sep_ms: float = _CANDIDATE_MIN_SEPARATION_MS,
                             window_ms: float = _ONSET_WINDOW_MS,
                             threshold_factor: float = _ONSET_THRESHOLD_FACTOR,
                             margin_ms: float = _ONSET_REFINE_MARGIN_MS) -> list:
    """Return up to k candidate onset sample indices, loudest-first.

    detect_onset() trusts the single global energy peak, which fails when a
    competing transient (echo, wind, insects) is comparably loud to the real
    event — there is no way to tell them apart from node A's recording
    alone. This returns several candidates so the caller (run_clap_test.py)
    can test each one against node B's audio and pick whichever actually
    correlates, instead of guessing from amplitude alone.
    """
    rms = _energy_envelope(data, rate, window_ms)
    background = np.median(rms)
    threshold = max(background * threshold_factor, 1e-6)
    min_sep = max(1, int(round(min_sep_ms * 1e-3 * rate)))
    margin = max(1, int(round(margin_ms * 1e-3 * rate)))

    work = rms.copy()
    candidates = []
    for _ in range(k):
        idx = int(np.argmax(work))
        if work[idx] <= threshold:
            break
        candidates.append(_refine_to_steepest_rise(data, idx, margin))
        lo, hi = max(0, idx - min_sep), min(len(work), idx + min_sep + 1)
        work[lo:hi] = -1.0

    if not candidates:
        raise ValueError(f"no transient found above {threshold_factor}x background RMS "
                          f"(background={background:.1f})")
    return candidates


# What first looked like a ~577Hz ambient interference tone (real-hardware
# testing, 2026-06-27) turned out to be the clap's own acoustic/mechanical
# resonance ring-down, not a separate noise source: isolating that frequency
# band in a quiet stretch of audio measured near-zero energy, but the same
# band during a clap's decay tail accounted for almost all of the tail's
# energy. A high-pass/notch filter can't separate this from the clap, since
# they occupy the same band — but correlating the full onset window lets
# cross-correlation lock onto whichever point in the two nodes' independent,
# quasi-periodic ring-downs happens to line up best, instead of the true
# direct-path arrival at the attack edge. That's what produced the
# persistently low quality ratios and the "everything converges on the same
# absolute point" pattern seen across many real-hardware runs. Restricting
# correlation to a short window right at the attack edge — before the
# ring-down has time to dominate — avoids the problem at the source instead
# of trying to filter it out.
_LEADING_EDGE_PRE_MS = 1.0
_LEADING_EDGE_POST_MS = 4.0


def trim_to_leading_edge(a: np.ndarray, b: np.ndarray, rate: int, center_idx: int,
                          pre_ms: float = _LEADING_EDGE_PRE_MS,
                          post_ms: float = _LEADING_EDGE_POST_MS) -> tuple:
    """Trim two onset-aligned slices down to a short window spanning pre_ms
    before to post_ms after the shared center_idx (the detected attack
    edge), discarding everything else — see module-level comment above for
    why. Both arrays are assumed to share the same sample index for a given
    moment in absolute time (true for two pulls of the same [tStart, tEnd]
    window, and for run_clap_test.py's onset-centered candidate slices).
    """
    pre = int(round(pre_ms * 1e-3 * rate))
    post = int(round(post_ms * 1e-3 * rate))
    lo = max(0, center_idx - pre)
    hi = min(len(a), len(b), center_idx + post)
    return a[lo:hi], b[lo:hi]


def _score_correlation(rate: int, a: np.ndarray, b: np.ndarray) -> dict:
    """Cross-correlate a against b and return the lag/quality numbers without
    printing anything. Split out from _correlate_and_report() so callers
    that need to rank several candidate slices (run_clap_test.py's
    multi-candidate onset-refine mode) can score each one cheaply and only
    print the full report for the winner.
    """
    corr = correlate(a, b, mode="full")
    peak_idx = int(np.argmax(corr))
    # lag_samples > 0 means b is delayed relative to a (b's clap arrives later in the buffer)
    lag_samples = peak_idx - (len(b) - 1)
    lag_samples += _parabolic_peak(corr, peak_idx)
    lag_us = lag_samples * 1e6 / rate

    quality_ratio, second_idx = _peak_quality(corr, peak_idx, rate)
    second_lag_samples = (second_idx - (len(b) - 1)) + _parabolic_peak(corr, second_idx)
    second_lag_us = second_lag_samples * 1e6 / rate

    a_energy = float(np.sum(a.astype(np.float64) ** 2))
    b_energy = float(np.sum(b.astype(np.float64) ** 2))
    denom = np.sqrt(a_energy * b_energy)
    peak_corr_coef = float(corr[peak_idx] / denom) if denom > 0 else 0.0

    return {
        "lag_samples": lag_samples,
        "lag_us": lag_us,
        "quality_ratio": quality_ratio,
        "second_lag_us": second_lag_us,
        "peak_corr_coef": peak_corr_coef,
    }


def _correlate_and_report(rate: int, a: np.ndarray, b: np.ndarray,
                           offset_us: float = 0.0, plot_path: str = None,
                           score: dict = None) -> float:
    """Core cross-correlation + reporting logic. Shared by analyze() (whole-
    file mode) and run_clap_test.py's onset-windowed mode, which calls this
    directly with a short slice around a detected transient instead of two
    full-length WAVs.

    Pass `score` (from _score_correlation()) to skip recomputing the
    correlation when the caller already scored this exact pair, e.g. after
    picking it as the best of several candidates.
    """
    if score is None:
        score = _score_correlation(rate, a, b)
    lag_samples = score["lag_samples"]
    lag_us = score["lag_us"]
    quality_ratio = score["quality_ratio"]
    second_lag_us = score["second_lag_us"]
    peak_corr_coef = score.get("peak_corr_coef")
    sync_error_us = lag_us - offset_us

    print(f"  Sample rate         : {rate} Hz")
    print(f"  Raw waveform lag    : {lag_samples:+.2f} samples ({lag_us:+.1f} us)")
    if offset_us:
        print(f"  Requested-window offset correction: {offset_us:+.1f} us")
    if np.isfinite(quality_ratio):
        print(f"  Peak quality        : {quality_ratio:.2f}x next-best "
              f"(runner-up at {second_lag_us:+.1f} us)")
    if peak_corr_coef is not None:
        print(f"  Peak corr. coef.    : {peak_corr_coef:.2f} "
              f"(normalized, 1.0 = perfect match)")
    if quality_ratio < _AMBIGUOUS_RATIO_THRESHOLD:
        print(f"  [WARN] Ambiguous correlation peak (ratio {quality_ratio:.2f} < "
              f"{_AMBIGUOUS_RATIO_THRESHOLD}) — a competing event (echo, ambient noise) is "
              f"nearly as strong as the chosen peak. This result may not reflect the actual "
              f"clap; treat it as unreliable rather than as a real sync-error measurement.")
    if peak_corr_coef is not None and peak_corr_coef < _MIN_PEAK_CORR_COEF:
        print(f"  [WARN] Weak correlation peak (coef {peak_corr_coef:.2f} < "
              f"{_MIN_PEAK_CORR_COEF}) — too little shared energy/structure between the two "
              f"slices to trust this match, regardless of quality_ratio (a high or even "
              f"'inf' ratio here likely just means there was nothing to compete with, not "
              f"that the alignment is correct).")
    print()
    if sync_error_us >= 0:
        print(f"  >>> Node B's clock is {sync_error_us:.1f} us BEHIND node A "
              f"(B's clap arrived late relative to A's audio).")
    else:
        print(f"  >>> Node B's clock is {abs(sync_error_us):.1f} us AHEAD of node A "
              f"(B's clap arrived early relative to A's audio).")
    print()
    print(f"  Inter-node sync error: {sync_error_us:+.1f} us")

    if plot_path:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            print("  [warn] matplotlib not installed — skipping --plot", file=sys.stderr)
            return sync_error_us

        shift = int(round(lag_samples))
        if shift >= 0:
            b_aligned = np.pad(b, (shift, 0))[: len(a)]
        else:
            b_aligned = b[-shift:][: len(a)]

        t = np.arange(len(a)) / rate
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(t, a / np.max(np.abs(a)), label="Node A", alpha=0.7)
        ax.plot(t[: len(b_aligned)], b_aligned / np.max(np.abs(b_aligned)),
                label="Node B (aligned)", alpha=0.7)
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Normalized amplitude")
        ax.set_title(f"Clap alignment — sync error {sync_error_us:+.1f} us")
        ax.legend()
        fig.tight_layout()
        fig.savefig(plot_path, dpi=150)
        print(f"\n  Plot saved: {plot_path}")

    return sync_error_us


def analyze(wav_a: str, wav_b: str, offset_us: float = 0.0, plot_path: str = None) -> float:
    """Cross-correlate two WAVs of the same clap; print and return inter-node sync error (us).

    Whole-file mode: loads both WAVs in full. Both pulls use the same
    [tStart, tEnd] window, so a's and b's sample indices line up directly —
    this locates the clap's attack edge in a via detect_onset() and scores
    the correlation off a short leading-edge window (see
    trim_to_leading_edge()) instead of the full buffer, to avoid the clap's
    own resonance ring-down dominating the result. Falls back to whole-buffer
    scoring (the old behavior) if no clear transient is found. The plot (if
    requested) still shows the full buffers for context, aligned by whichever
    lag was actually used for the score.

    For the onset-windowed mode (a short slice around a detected transient
    instead of two full files), call _correlate_and_report() directly — see
    run_clap_test.py.
    """
    rate_a, a = _load_mono(wav_a)
    rate_b, b = _load_mono(wav_b)

    if rate_a != rate_b:
        # Resample b onto a's rate so correlation lag is in a's sample units.
        from math import gcd
        g = gcd(rate_a, rate_b)
        b = resample_poly(b, rate_a // g, rate_b // g)
        print(f"  [note] resampled node B from {rate_b} Hz to {rate_a} Hz")
    rate = rate_a

    score = None
    try:
        onset_idx = detect_onset(a, rate)
        trimmed_a, trimmed_b = trim_to_leading_edge(a, b, rate, onset_idx)
        score = _score_correlation(rate, trimmed_a, trimmed_b)
    except ValueError as e:
        print(f"  [note] no clear onset found ({e}) — falling back to whole-buffer correlation")

    return _correlate_and_report(rate, a, b, offset_us, plot_path, score=score)


def main():
    parser = argparse.ArgumentParser(
        description="Cross-correlate two node recordings of the same clap to measure inter-node sync error.")
    parser.add_argument("wav_a", help="WAV pulled from node A")
    parser.add_argument("wav_b", help="WAV pulled from node B")
    parser.add_argument("--offset-us", type=float, default=0.0,
                         help="Known (tStart_b - tStart_a) in microseconds, if the two pulls "
                              "did not use an identical request window. Default 0 (same window).")
    parser.add_argument("--plot", metavar="PNG_PATH", default=None,
                         help="Save an alignment plot to this path (requires matplotlib).")
    args = parser.parse_args()
    analyze(args.wav_a, args.wav_b, args.offset_us, args.plot)


if __name__ == "__main__":
    main()
