"""
diag_hum_check.py — One-off diagnostic: is onset-refine locking onto periodic
interference (mains hum / switching noise) instead of the real clap?

Context: run_clap_test.py's multi-candidate onset-refine mode has repeatedly
shown a suspicious pattern across several real-hardware runs — for every
candidate tried, (onset_time_into_A + reported_lag) converges on nearly the
same fixed point in absolute time, regardless of which candidate (i.e. which
absolute window) was requested from node B, while the correlation quality
stays low (~1.0-1.1x). A real clap's lag should track WHICH window you asked
for; a fixed-phase periodic noise source (50/100Hz mains hum, switching
regulator ripple, etc.) would instead always correlate best at the same
absolute phase no matter which window you sliced, and would produce many
near-equal correlation peaks (one per cycle) -> low quality ratio.

First pass of this script (scan under 1kHz) found a ~577Hz tone dominating
the whole spectrum. A high-pass at 1kHz was tried as a fix in
clap_sync_check.py but did NOT resolve the problem — ground-truth quality
even dropped slightly. That's consistent with the ~577Hz component being a
periodic CLICK rather than a clean sine tone: a click has a comb of harmonics
(2x, 3x, 4x, ... the fundamental) extending well above 1kHz, directly
overlapping the clap's own frequency range, so a simple high-pass can't
separate them. This second pass checks that directly: scans the full
spectrum (not just <1kHz) for a harmonic series built on the fundamental, and
reports the crest factor (peak/RMS) as a cheap smooth-tone-vs-spiky-click
discriminator (a clean sine has crest factor ~1.4; a narrow periodic click
concentrates energy into spikes and reads much higher).

Not part of the regular clap-sync tooling — throwaway script for this one
question.

Usage:
    python tools/diag_hum_check.py audio/audio_<id>_<mac>.wav [more.wav ...]

Requires: numpy, scipy. Run from sound-hub root.
"""

import argparse

import numpy as np
from scipy.io import wavfile

# Mains hum and most switching-supply ripple shows up well under 1kHz;
# clap energy is broadband and much higher up, so this range is specific
# enough to flag a hum-like component without false-positiving on the clap.
_SCAN_MAX_HZ = 1000.0
_MIN_HZ = 3.0  # skip near-DC drift
_TOP_N = 6

# Full-spectrum scan range and harmonic series check, to see how far a comb
# built on the fundamental extends — and whether it reaches into the clap's
# own frequency range (roughly 1-8kHz for a sharp handclap).
_FULL_SCAN_MAX_HZ = 12000.0
_N_HARMONICS = 10
_HARMONIC_TOLERANCE_HZ = 5.0  # search window around each exact multiple, to allow for the ~577 vs 579Hz drift already observed


def _load_mono(path: str):
    rate, data = wavfile.read(path)
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = data.astype(np.float64)
    data -= data.mean()
    return rate, data


def _spectrum(data: np.ndarray, rate: int):
    # Hann window to reduce spectral leakage from the block edges. Shared by
    # _top_peaks() and _harmonic_series() so both look at the same FFT.
    windowed = data * np.hanning(len(data))
    spectrum = np.abs(np.fft.rfft(windowed))
    freqs = np.fft.rfftfreq(len(data), d=1.0 / rate)
    return freqs, spectrum


def _top_peaks(freqs: np.ndarray, spectrum: np.ndarray, max_hz: float, min_hz: float, n: int):
    band = (freqs >= min_hz) & (freqs <= max_hz)
    band_freqs = freqs[band]
    band_spec = spectrum[band]

    if len(band_spec) == 0:
        return []

    order = np.argsort(band_spec)[::-1][:n]
    overall_max = spectrum.max() if spectrum.max() > 0 else 1.0
    return [(band_freqs[i], band_spec[i], band_spec[i] / overall_max) for i in order]


def _harmonic_series(freqs: np.ndarray, spectrum: np.ndarray, fundamental_hz: float,
                      n_harmonics: int, tolerance_hz: float, max_hz: float):
    """For each multiple of fundamental_hz up to max_hz, find the strongest
    bin within tolerance_hz of the exact multiple and report it relative to
    the fundamental's own magnitude. A click-like artifact shows a comb of
    harmonics that stay a substantial fraction of the fundamental's strength
    well past 1kHz; a clean sine tone's harmonics (if any — a pure sine has
    none) drop away immediately.
    """
    fundamental_mag = None
    results = []
    for k in range(1, n_harmonics + 1):
        target_hz = fundamental_hz * k
        if target_hz > max_hz:
            break
        window = (freqs >= target_hz - tolerance_hz) & (freqs <= target_hz + tolerance_hz)
        if not np.any(window):
            results.append((k, target_hz, None, None))
            continue
        local_freqs = freqs[window]
        local_spec = spectrum[window]
        best = int(np.argmax(local_spec))
        mag = local_spec[best]
        if k == 1:
            fundamental_mag = mag
        ratio = (mag / fundamental_mag) if fundamental_mag else None
        results.append((k, local_freqs[best], mag, ratio))
    return results


def _crest_factor(data: np.ndarray) -> float:
    """peak/RMS amplitude ratio — cheap discriminator between a smooth tone
    (sine ~1.41) and a narrow periodic click (energy concentrated in spikes,
    reads much higher).
    """
    rms = np.sqrt(np.mean(data.astype(np.float64) ** 2))
    if rms == 0:
        return float("nan")
    return float(np.max(np.abs(data)) / rms)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wavs", nargs="+", help="WAV file(s) to scan")
    args = parser.parse_args()

    for path in args.wavs:
        rate, data = _load_mono(path)
        duration_s = len(data) / rate
        print(f"--- {path} ---")
        print(f"  {rate} Hz, {len(data)} samples, {duration_s:.4f}s")
        print(f"  Crest factor (peak/RMS): {_crest_factor(data):.2f}  "
              f"(~1.41 = clean sine, much higher = spiky/click-like)")

        freqs, spectrum = _spectrum(data, rate)

        peaks = _top_peaks(freqs, spectrum, _SCAN_MAX_HZ, _MIN_HZ, _TOP_N)
        if not peaks:
            print("  (no usable spectrum)")
            print()
            continue

        print(f"  Top {len(peaks)} peaks under {_SCAN_MAX_HZ:.0f}Hz "
              f"(freq, magnitude, fraction of overall spectrum peak):")
        for freq, mag, frac in peaks:
            flag = ""
            if abs(freq - 50.0) < 2.0 or abs(freq - 100.0) < 2.0 or abs(freq - 150.0) < 2.0:
                flag = "  <- near 50Hz mains harmonic"
            elif abs(freq - 60.0) < 2.0 or abs(freq - 120.0) < 2.0:
                flag = "  <- near 60Hz mains harmonic"
            print(f"    {freq:8.2f} Hz   mag={mag:12.1f}   frac={frac:.3f}{flag}")

        fundamental_hz = peaks[0][0]
        nyquist = rate / 2.0
        full_max_hz = min(_FULL_SCAN_MAX_HZ, nyquist)
        harmonics = _harmonic_series(freqs, spectrum, fundamental_hz, _N_HARMONICS,
                                      _HARMONIC_TOLERANCE_HZ, full_max_hz)
        print(f"  Harmonic series of {fundamental_hz:.2f}Hz fundamental, up to "
              f"{full_max_hz:.0f}Hz (magnitude relative to fundamental):")
        for k, freq, mag, ratio in harmonics:
            if mag is None:
                print(f"    {k}x ({k * fundamental_hz:.0f}Hz): out of range / no bin found")
                continue
            in_clap_band = "  <- inside typical clap frequency range" if freq >= 1000 else ""
            print(f"    {k}x  {freq:8.2f} Hz   mag={mag:12.1f}   ratio={ratio:.3f}{in_clap_band}")
        print()

    print("Interpretation:")
    print("  - Crest factor near 1.4: smooth tone (e.g. mains hum) -> a clean high-pass or")
    print("    notch at the fundamental should fully remove it.")
    print("  - Crest factor much higher (3+): spiky/click-like artifact -> expect a strong")
    print("    harmonic comb extending well above the fundamental; if ratios above stay")
    print("    non-trivial (e.g. >0.1) past 1kHz, the artifact overlaps the clap's own")
    print("    band and simple frequency-domain filtering can't cleanly separate them —")
    print("    a different approach (e.g. time-domain glitch removal, or fixing the noise")
    print("    source itself) would be needed instead.")


if __name__ == "__main__":
    main()
