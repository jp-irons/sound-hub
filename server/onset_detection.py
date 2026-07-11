"""
Onset detection for TDOA correlation (species_tdoa_pipeline design,
sound-hub/DESIGN.md, milestone 3).

detect_onset() and its helpers below are copied from tools/clap_sync_check.py
(the same "global_peak" transient detector validated against real hand-clap
field tests — see DESIGN.md's milestone 5 note: "only `global_peak` exists
today (matches `clap_sync_check.py`'s `detect_onset`)"). Do not edit the
algorithm here without also updating tools/clap_sync_check.py (or vice
versa) — same copy-don't-diverge convention tdoa_solver.py uses for the
solver itself. tools/ is a standalone operator-run diagnostic script, not a
production dependency of the server, so the implementation is duplicated
here rather than imported across that boundary.

Known gap (not solved here): no species-matched bandpass filtering is
applied before onset detection. docs/tdoa-correlation-design-notes.md found
that narrow-window onset detection without a bandpass filter misses 71-78%
of real bird calls in the 0-15dB SNR range that most real detections fall
into — bandpass recovery of that miss rate is a validated finding but not
yet wired into any production code path, here included. Species like
Pheasant Coucal (short, smooth, low-frequency) are not reliably detected by
this method at all — see that doc's "open exception" section.
"""

from __future__ import annotations

import numpy as np
import soundfile as sf

# Onset detector tuning — short-time energy envelope, refined to the
# steepest-rise sample. Copied from tools/clap_sync_check.py; keep in sync
# manually if that file's tuning changes (see module docstring).
_ONSET_WINDOW_MS = 3.0          # short-time energy averaging window
_ONSET_THRESHOLD_FACTOR = 8.0   # multiple of background RMS the peak must exceed
_ONSET_REFINE_MARGIN_MS = 5.0   # +/- search margin around the peak for steepest-rise refinement


def _load_mono(filepath: str) -> tuple[int, np.ndarray]:
    """Read a WAV file as mono float64, mean-subtracted — mirrors
    tools/clap_sync_check.py's _load_mono, but via soundfile (already a
    server dependency) rather than scipy.io.wavfile."""
    data, rate = sf.read(filepath, always_2d=False)
    data = np.asarray(data)
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = data.astype(np.float64)
    data -= data.mean()
    return rate, data


def _energy_envelope(data: np.ndarray, rate: int, window_ms: float) -> np.ndarray:
    win = max(1, int(round(window_ms * 1e-3 * rate)))
    energy = np.convolve(data.astype(np.float64) ** 2, np.ones(win) / win, mode="same")
    return np.sqrt(energy)


def _refine_to_steepest_rise(data: np.ndarray, idx: int, margin_samples: int) -> int:
    """Refine a coarse energy-envelope peak index to the steepest-rise raw
    sample within margin_samples of it."""
    lo = max(0, idx - margin_samples)
    hi = min(len(data), idx + margin_samples)
    if hi - lo <= 1:
        return idx
    segment = np.abs(data[lo:hi].astype(np.float64))
    return lo + int(np.argmax(np.diff(segment)))


def detect_onset(
    data: np.ndarray, rate: int,
    window_ms: float = _ONSET_WINDOW_MS,
    threshold_factor: float = _ONSET_THRESHOLD_FACTOR,
    margin_ms: float = _ONSET_REFINE_MARGIN_MS,
) -> int:
    """Return the sample index of the most prominent short-time-energy
    transient in `data`, refined to the steepest-rise sample within
    margin_ms of it.

    Takes the GLOBAL peak of the energy envelope, not the first sample that
    crosses threshold_factor x background RMS — see
    tools/clap_sync_check.py's detect_onset docstring for the full
    rationale (a real outdoor buffer often has other transients ahead of
    the one that matters).

    Raises ValueError if even the loudest point in the buffer doesn't look
    like a real transient. The message includes enough detail (background/
    peak RMS at higher precision than the old 1-decimal format — which
    rounded any background/peak below 0.05 to a misleading "0.0" — plus
    buffer duration and raw peak sample amplitude) to diagnose the failure
    from the tdoa_attempt_nodes.error column alone, without needing to pull
    the WAV file itself. Added 2026-07-11 after exactly that ambiguity came
    up in practice — "background=0.0, peak=0.0" looked like a bug in the
    onset detector when it was actually a real near-silent buffer.
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


def detect_onset_us(method: str, filepath: str, t_start_us: int) -> float:
    """Run the species' configured onset_detection_method against a node's
    WAV and return the absolute (node-clock) microsecond timestamp of the
    detected onset.

    t_start_us must be the actual capture-window start for this specific
    file (audio_events.t_start_us / the push's tStartUs) — the onset sample
    index is relative to the start of the buffer, not to the attempt's
    padded pull window, so using the wrong start here would silently shift
    every arrival time by a constant offset.

    Raises ValueError if the method is unknown or no transient is found;
    callers should catch this and record 'onset_failed' rather than let it
    propagate (see routes.py _correlate_attempt_node).
    """
    if method != "global_peak":
        raise ValueError(f"unknown onset_detection_method '{method}'")

    rate, data = _load_mono(filepath)
    onset_idx = detect_onset(data, rate)
    return t_start_us + (onset_idx / rate) * 1e6
