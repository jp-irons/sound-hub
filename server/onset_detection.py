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

Bandpass filtering (added 2026-07-11): docs/tdoa-correlation-design-notes.md
found that narrow-window onset detection without a bandpass filter misses
71-78% of real bird calls in the 0-15dB SNR range that most real detections
fall into — bandpass recovery of that miss rate is a validated finding
(tools/synthetic_snr_feasibility.py), and bandpass_filter() below is that
same validated implementation, now wired into detect_onset_us(). It's still
a per-species opt-in, though: species_tdoa_params.freq_band_low_hz/high_hz
default to NULL (no filtering) for any species that hasn't had its call
band characterized from a reference recording yet, so this is plumbing, not
an automatic fix — see get_effective_species_tdoa_params() in db.py.
Species like Pheasant Coucal (short, smooth, low-frequency) are not
reliably detected by the onset detector at all even with filtering — see
that doc's "open exception" section.

onset_threshold_factor (added 2026-07-11): also now a per-species,
DB-tunable parameter (species_tdoa_params.onset_threshold_factor) rather
than a hardcoded constant — see SpeciesTdoaParams.onset_threshold_factor in
models.py for the full rationale on why 8.0 remains the factory default and
why lowering it isn't a substitute for bandpass filtering.
"""

from __future__ import annotations

import logging

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfiltfilt

log = logging.getLogger("sound_hub.onset_detection")

# Onset detector tuning — short-time energy envelope, refined to the
# steepest-rise sample. Copied from tools/clap_sync_check.py; keep in sync
# manually if that file's tuning changes (see module docstring). These are
# the last-resort Python-level defaults used only if a caller doesn't pass
# an explicit value — in production, routes.py always passes the
# per-species DB-resolved values through (species_tdoa_params, or its
# FACTORY_DEFAULT_SPECIES_PARAMS fallback in db.py if the DB row is missing/
# disabled), so these constants exist as a second, redundant safety net,
# not the live source of truth.
_ONSET_WINDOW_MS = 3.0          # short-time energy averaging window
_ONSET_THRESHOLD_FACTOR = 8.0   # multiple of background RMS the peak must exceed
_ONSET_REFINE_MARGIN_MS = 5.0   # +/- search margin around the peak for steepest-rise refinement
_BANDPASS_ORDER = 4             # Butterworth filter order, matches synthetic_snr_feasibility.py


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


def bandpass_filter(
    data: np.ndarray, rate: int, low_hz: float, high_hz: float,
    order: int = _BANDPASS_ORDER,
) -> np.ndarray:
    """Zero-phase Butterworth bandpass (sosfiltfilt — no group delay, so it
    doesn't itself bias the TDOA estimate). Ported from
    tools/synthetic_snr_feasibility.py's bandpass_filter(), the
    implementation that validated bandpass recovery of the onset-detection
    miss rate at realistic SNR — see module docstring. Keep in sync
    manually if that file's implementation changes, same copy-don't-diverge
    convention as detect_onset() and clap_sync_check.py.

    Intended to be applied to the full buffer before detect_onset(), using a
    per-species band derived from a reference recording (once one exists —
    species_tdoa_params.freq_band_low_hz/high_hz has no per-species values
    populated yet as of this writing).
    """
    nyq = rate / 2.0
    low = max(low_hz / nyq, 1e-4)
    high = min(high_hz / nyq, 0.999)
    sos = butter(order, [low, high], btype="band", output="sos")
    return sosfiltfilt(sos, data)


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
    background_override: float | None = None,
) -> int:
    """Return the sample index of the most prominent short-time-energy
    transient in `data`, refined to the steepest-rise sample within
    margin_ms of it.

    Takes the GLOBAL peak of the energy envelope, not the first sample that
    crosses threshold_factor x background RMS — see
    tools/clap_sync_check.py's detect_onset docstring for the full
    rationale (a real outdoor buffer often has other transients ahead of
    the one that matters).

    background_override (added 2026-07-19, node-reported noise floor
    design; changed to a min()-based safety floor 2026-07-21 — see below):
    when given, the LOWER of this and the clip-derived median is used as
    the background level. Motivation: TDOA corroboration pulls are
    deliberately narrow (pre/post margins around a known arrival — see
    routes.py _plan_tdoa_attempt_inner), so a clip can be mostly "active
    signal" with too little genuine quiet background left for the
    clip-derived median to be reliable — the node tracks its own ambient
    level continuously (NoiseFloorTracker, sound-capture-node) and reports
    it on every pull. Must already be in the same normalized [-1.0, 1.0)
    scale soundfile uses here — see detect_onset_us's node_noise_floor_rms
    param for where the int16->normalized conversion happens.

    **2026-07-21 correction:** originally always preferred the override
    outright when present. Real fleet-wide field data (dawn+dusk cycle,
    717 pulled-neighbor rows) showed this was actively harmful — onset_failed
    on pulled-neighbor rows hit 89.7% (vs ~1-3% for origin/unaffected rows),
    because node_reported was higher than clip_derived 94.8% of the time,
    not lower as the original design assumed. Root cause: NoiseFloorTracker
    integrates over a 180s window; during sustained chorus activity, many
    calls sit below its freeze-ratio threshold and gradually drag the floor
    up toward "typical chorus-period activity" rather than true quiet
    ambient, while a short clip's own median (centered on one specific
    arrival) stays closer to the quiet moments immediately around that one
    call — the opposite of the narrow-clip-inflation effect this was built
    to counter. Switching to min() is a safety measure, not a fix for that
    underlying tau/freeze mismatch: it guarantees this can never raise the
    effective threshold above what clip-derived-only behavior already gave,
    so it can only help or be a no-op, never actively hurt, while the
    NoiseFloorTracker measurement itself gets redesigned. The clip-derived
    median is still computed and logged alongside the override every time
    one is given — see project_bird_noise_floor_reporting memory for the
    full investigation. Not yet used to change any margins itself.

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
    clip_background = np.median(rms)
    if background_override is not None:
        background = min(clip_background, background_override)
        log.info(
            "onset background: clip_derived=%.5f node_reported=%.5f "
            "delta=%.5f (using %s)",
            clip_background, background_override,
            background_override - clip_background,
            "node_reported" if background < clip_background else "clip_derived",
        )
    else:
        background = clip_background
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


def detect_onset_us(
    method: str,
    filepath: str,
    t_start_us: int,
    threshold_factor: float = _ONSET_THRESHOLD_FACTOR,
    freq_band_low_hz: float | None = None,
    freq_band_high_hz: float | None = None,
    node_noise_floor_rms: float | None = None,
) -> float:
    """Run the species' configured onset_detection_method against a node's
    WAV and return the absolute (node-clock) microsecond timestamp of the
    detected onset.

    t_start_us must be the actual capture-window start for this specific
    file (audio_events.t_start_us / the push's tStartUs) — the onset sample
    index is relative to the start of the buffer, not to the attempt's
    padded pull window, so using the wrong start here would silently shift
    every arrival time by a constant offset.

    threshold_factor/freq_band_low_hz/freq_band_high_hz should be the
    per-species values snapshotted onto the tdoa_attempts row at plan time
    (see routes.py _plan_tdoa_attempt_inner) — callers should always pass
    them explicitly rather than relying on this function's defaults, which
    exist only as a last-resort fallback (see module docstring). The
    bandpass filter only runs when BOTH freq_band_low_hz and
    freq_band_high_hz are given — either one alone (or neither) leaves the
    buffer unfiltered, matching today's default no-filtering behavior.

    node_noise_floor_rms (added 2026-07-19): raw int16-scale broadband RMS
    noise floor as reported by the node on this pull (AudioPullHandler's
    X-Noise-Floor-Rms header, NoiseFloorTracker on the node side — see that
    class's doc for what it measures). None for callers that don't have a
    pull response to read a header from (origin/known-reporter WAVs, or
    older node firmware) — see detect_onset's background_override doc for
    what happens in that case. The conversion from this raw int16 scale to
    the normalized [-1.0, 1.0) scale soundfile loads WAVs into lives here,
    not in the caller, so routes.py never needs to know soundfile's
    normalization convention.

    Raises ValueError if the method is unknown or no transient is found;
    callers should catch this and record 'onset_failed' rather than let it
    propagate (see routes.py _correlate_attempt_node).
    """
    if method != "global_peak":
        raise ValueError(f"unknown onset_detection_method '{method}'")

    rate, data = _load_mono(filepath)
    if freq_band_low_hz is not None and freq_band_high_hz is not None:
        data = bandpass_filter(data, rate, freq_band_low_hz, freq_band_high_hz)
    background_override = (
        node_noise_floor_rms / 32768.0 if node_noise_floor_rms is not None else None
    )
    onset_idx = detect_onset(
        data, rate, threshold_factor=threshold_factor,
        background_override=background_override,
    )
    return t_start_us + (onset_idx / rate) * 1e6
