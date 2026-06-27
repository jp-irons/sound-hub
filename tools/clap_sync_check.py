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

    rate_a, a = _load_mono(args.wav_a)
    rate_b, b = _load_mono(args.wav_b)

    if rate_a != rate_b:
        # Resample b onto a's rate so correlation lag is in a's sample units.
        from math import gcd
        g = gcd(rate_a, rate_b)
        b = resample_poly(b, rate_a // g, rate_b // g)
        print(f"  [note] resampled node B from {rate_b} Hz to {rate_a} Hz")
    rate = rate_a

    corr = correlate(a, b, mode="full")
    peak_idx = int(np.argmax(corr))
    # lag_samples > 0 means b is delayed relative to a (b's clap arrives later in the buffer)
    lag_samples = peak_idx - (len(b) - 1)
    lag_samples += _parabolic_peak(corr, peak_idx)

    lag_us = lag_samples * 1e6 / rate
    sync_error_us = lag_us - args.offset_us

    print(f"  Sample rate         : {rate} Hz")
    print(f"  Raw waveform lag    : {lag_samples:+.2f} samples ({lag_us:+.1f} us)")
    if args.offset_us:
        print(f"  Requested-window offset correction: {args.offset_us:+.1f} us")
    print()
    if sync_error_us >= 0:
        print(f"  >>> Node B's clock is {sync_error_us:.1f} us BEHIND node A "
              f"(B's clap arrived late relative to A's audio).")
    else:
        print(f"  >>> Node B's clock is {abs(sync_error_us):.1f} us AHEAD of node A "
              f"(B's clap arrived early relative to A's audio).")
    print()
    print(f"  Inter-node sync error: {sync_error_us:+.1f} us")

    if args.plot:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            print("  [warn] matplotlib not installed — skipping --plot", file=sys.stderr)
            return

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
        fig.savefig(args.plot, dpi=150)
        print(f"\n  Plot saved: {args.plot}")


if __name__ == "__main__":
    main()
