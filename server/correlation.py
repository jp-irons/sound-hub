"""
correlation.py — leading-edge cross-correlation refinement for TDOA arrival
times (species_tdoa_pipeline design, sound-hub/DESIGN.md, milestone 5).

Ported from tools/clap_sync_check.py's trim_to_leading_edge()/
_score_correlation() (same "copy don't diverge" convention as
onset_detection.py uses for detect_onset() — see that module's docstring).

Why this exists: onset_detection.detect_onset_us() is run independently per
node — an amplitude/energy-threshold detector reading each node's own copy
of the call in isolation. Two nodes at different distances from the same
call hear it at different loudness/SNR, and a call's energy envelope often
keeps rising well past its true onset (docs/tdoa-correlation-design-notes.md
sections 1-3) — an independent per-node detector is exposed to that
asymmetry: the louder/closer node's envelope rises faster and can appear to
"peak" at a different point relative to the true transient than the
quieter/farther node's. Cross-correlating a short shared window instead
compares *waveform shape* between the two nodes' copies of the same
transient, which is far less sensitive to which channel happens to be
louder or carries more "active" signal — see
project_bird_tdoa_correlation_gap memory for the full investigation that
led here.

Unlike clap_sync_check.py's version, correlate_leading_edge() below does
NOT assume both buffers share a common sample-index timebase. In
production the origin's WAV is the raw triggering push (its own local
capture window) while a neighbour's WAV is pulled later against a
different window (origin_arrival_us +/- the species' pull margins — see
routes.py _plan_tdoa_attempt_inner). Both t_start_us values are absolute
GPS-PPS-referenced microsecond timestamps though, so each buffer's trim
center is computed independently from its own t_start_us against the same
origin_arrival_us anchor, rather than assuming a shared sample index.
"""

from __future__ import annotations

from math import gcd

import numpy as np
from scipy.signal import correlate, resample_poly

# Leading-edge trim window — deliberately narrow, matching
# tools/clap_sync_check.py's clap-tuned defaults. tdoa-correlation-design-
# notes.md validated this width (not wider) as the safer production choice
# at real-world SNR (see that doc's sections 3 and 8 — widening only pays
# off once SNR is already high, and has a much heavier error tail even with
# bandpass filtering). Do not widen without re-running that validation.
LEADING_EDGE_PRE_MS = 1.0
LEADING_EDGE_POST_MS = 4.0

# Confidence gate. MIN_PEAK_CORR_COEF is still the value validated in
# tools/clap_sync_check.py against real hand-clap field tests (see that
# module's _MIN_PEAK_CORR_COEF for the reasoning/incident it traces back
# to) — field data (project_bird_tdoa_implausible_solves memory, 2026-07-23)
# shows it's doing real work: only 55 of 2040 fell-back rows failed on
# coefficient, averaging 0.214, clearly weak matches.
#
# AMBIGUOUS_RATIO_THRESHOLD lowered 1.5→1.2 (2026-07-23) — also inherited
# from clap_sync_check.py, but same field data showed it was the dominant
# bottleneck, not coefficient: 1985 of 2040 fell-back rows (97%) failed
# *only* on ambiguity, with peak_corr_coef averaging 0.798 — actually higher
# than the trusted group's own 0.704 average — and the rejected group's
# ambiguity ratio averaged 1.198, not far below the old 1.5 bar. Real bird
# calls plausibly have more repeated/periodic internal structure (harmonics,
# trills) than the hand claps this was tuned against, producing a genuine
# secondary correlation peak that isn't a wrong-match signal so much as a
# normal feature of the call shape. Loosening this is judged a favourable
# trade: LEADING_EDGE_PRE_MS/POST_MS bound the trim window to 5ms, so even a
# wrongly-chosen secondary peak within an ambiguous window is bounded to a
# few ms of timing error (~metres of range error at ~343 m/s) — small next
# to the alternative, which is falling back to the fully independent
# per-node detector with no cross-check at all (unbounded error — the
# likely source of the implausible, thousands-of-km TDOA solves that
# motivated this investigation). 1.2 is a first field-data-driven guess,
# not a validated final value — expect retuning from real post-change data,
# same status as every other threshold in this pipeline.
AMBIGUOUS_RATIO_THRESHOLD = 1.2
MIN_PEAK_CORR_COEF = 0.3

# How close (in samples) a secondary peak has to be to the primary one to be
# considered "the same event" rather than a competing one. Matches
# clap_sync_check.py's _PEAK_EXCLUSION_MS.
_PEAK_EXCLUSION_MS = 1.0

# Minimum trimmed-window length (each side) to attempt a correlation at
# all — below this there isn't enough audio left after trimming (buffer
# edge, or a badly wrong center) to say anything meaningful. Deliberately
# small (well under LEADING_EDGE_PRE_MS+POST_MS's ~5ms nominal width) so
# only genuinely degenerate trims are rejected here, not just short ones —
# the peak_corr_coef/quality_ratio gate is what should catch a poor-quality
# correlation on an otherwise normal-length window.
_MIN_TRIM_SAMPLES = 8


def _parabolic_peak(corr: np.ndarray, peak_idx: int) -> float:
    """Sub-sample peak refinement via parabolic interpolation. Identical to
    tools/clap_sync_check.py's _parabolic_peak() — keep in sync manually if
    that changes, same convention as onset_detection.py."""
    if peak_idx <= 0 or peak_idx >= len(corr) - 1:
        return 0.0
    y0, y1, y2 = corr[peak_idx - 1], corr[peak_idx], corr[peak_idx + 1]
    denom = y0 - 2 * y1 + y2
    if denom == 0:
        return 0.0
    return 0.5 * (y0 - y2) / denom


def _peak_quality(corr: np.ndarray, peak_idx: int, rate: int) -> float:
    """Ratio of the primary correlation peak to the next-highest peak found
    outside a small exclusion zone around it. Identical to
    tools/clap_sync_check.py's _peak_quality() (minus the second_idx return,
    unused here)."""
    exclude = max(1, int(round(_PEAK_EXCLUSION_MS * 1e-3 * rate)))
    masked = corr.astype(np.float64).copy()
    lo = max(0, peak_idx - exclude)
    hi = min(len(corr), peak_idx + exclude + 1)
    masked[lo:hi] = -np.inf
    second_idx = int(np.argmax(masked))
    second_val = masked[second_idx]
    peak_val = corr[peak_idx]
    if not np.isfinite(second_val) or second_val <= 0:
        return float("inf")
    return float(peak_val / second_val)


def _score_correlation(rate: int, a: np.ndarray, b: np.ndarray, method: str) -> dict:
    """Cross-correlate a against b and return lag/quality numbers.

    method is 'plain' (time-domain cross-correlation, weighted by actual
    signal energy) or 'gcc_phat' (per-bin phase normalization).
    tdoa-correlation-design-notes.md section 7 found plain clearly better
    once bandpass filtering is already applied upstream — PHAT's per-bin
    normalization re-amplifies the bandpass filter's residual stopband
    content back up to full weight, undoing much of what the filter bought.
    'plain' is the production default (species_tdoa_params.correlation_method)
    as of 2026-07-17; 'gcc_phat' remains implemented since the field is now
    actually read rather than dead config, and PHAT can still be the better
    choice for a species without a characterized frequency band (no
    bandpass applied at all).
    """
    if method == "gcc_phat":
        n = len(a) + len(b) - 1
        n_fft = 1
        while n_fft < n:
            n_fft *= 2
        A = np.fft.rfft(a, n_fft)
        B = np.fft.rfft(b, n_fft)
        R = A * np.conj(B)
        denom = np.abs(R)
        denom[denom == 0] = 1e-12
        R = R / denom
        corr_full = np.fft.irfft(R, n_fft)
        # Reassemble into the same [-(len(b)-1) .. +(len(a)-1)] layout
        # scipy.signal.correlate(mode="full") returns, so the lag math
        # below (peak_idx - (len(b) - 1)) is identical for both methods.
        corr = np.concatenate((corr_full[-(len(b) - 1):], corr_full[: len(a)]))
    elif method == "plain":
        corr = correlate(a, b, mode="full")
    else:
        raise ValueError(f"unknown correlation method '{method}'")

    peak_idx = int(np.argmax(corr))
    # lag_samples > 0 means b is delayed relative to a.
    lag_samples = peak_idx - (len(b) - 1)
    lag_samples += _parabolic_peak(corr, peak_idx)
    lag_us = lag_samples * 1e6 / rate

    quality_ratio = _peak_quality(corr, peak_idx, rate)

    a_energy = float(np.sum(a.astype(np.float64) ** 2))
    b_energy = float(np.sum(b.astype(np.float64) ** 2))
    denom = np.sqrt(a_energy * b_energy)
    peak_corr_coef = float(corr[peak_idx] / denom) if denom > 0 else 0.0

    return {
        "lag_us": lag_us,
        "quality_ratio": quality_ratio,
        "peak_corr_coef": peak_corr_coef,
    }


def _trim_leading_edge(
    data: np.ndarray, rate: int, center_idx: int,
    pre_ms: float = LEADING_EDGE_PRE_MS, post_ms: float = LEADING_EDGE_POST_MS,
) -> np.ndarray:
    """Trim one buffer to a short window spanning pre_ms before to post_ms
    after center_idx. Unlike tools/clap_sync_check.py's
    trim_to_leading_edge() (which trims two buffers around one shared
    sample index), this trims one buffer at a time — see
    correlate_leading_edge()'s docstring for why production needs a
    separately-computed center_idx per buffer."""
    pre = int(round(pre_ms * 1e-3 * rate))
    post = int(round(post_ms * 1e-3 * rate))
    lo = max(0, center_idx - pre)
    hi = min(len(data), center_idx + post)
    if hi <= lo:
        return data[0:0]
    return data[lo:hi]


def correlate_leading_edge(
    *,
    origin_data: np.ndarray, origin_rate: int, origin_t_start_us: float,
    origin_arrival_us: float,
    neighbor_data: np.ndarray, neighbor_rate: int, neighbor_t_start_us: float,
    method: str = "plain",
) -> dict | None:
    """Refine a neighbour node's arrival time by cross-correlating a short
    leading-edge window of its WAV against the same real-world window of
    the origin's WAV, both anchored on origin_arrival_us — already known
    from the origin's own independent onset detection at planning time
    (routes.py _fire_cluster_after_delay).

    origin_data/neighbor_data should already be bandpass-filtered to the
    species' band by the caller if one is configured (same filtering
    onset_detection.detect_onset_us applies before onset detection —
    tdoa-correlation-design-notes.md found bandpass helps plain correlation
    directly, not just onset detection).

    Returns None if either trimmed window ends up too short to correlate
    meaningfully (buffer edge, or a badly wrong center) — callers should
    treat None the same as a below-threshold "trusted" result: keep the
    independently-detected arrival_us rather than trust a correlated one.

    Otherwise returns a dict with:
        arrival_us:     origin_arrival_us + measured lag — the refined
                         estimate for when this transient reached the
                         neighbour, in the same absolute microsecond epoch
                         as origin_arrival_us.
        lag_us:         the measured lag itself (arrival_us - origin_arrival_us).
        peak_corr_coef: normalized correlation peak height, 0-1.
        quality_ratio:  primary/secondary peak ratio (inf if no competing peak).
        trusted:        True if peak_corr_coef/quality_ratio both clear this
                         module's MIN_PEAK_CORR_COEF/AMBIGUOUS_RATIO_THRESHOLD
                         gates. Numbers are returned either way so callers can
                         still log/compare a distrusted result.
    """
    if origin_rate != neighbor_rate:
        g = gcd(origin_rate, neighbor_rate)
        neighbor_data = resample_poly(neighbor_data, origin_rate // g, neighbor_rate // g)
        neighbor_rate = origin_rate

    rate = origin_rate
    origin_center = int(round((origin_arrival_us - origin_t_start_us) * 1e-6 * rate))
    neighbor_center = int(round((origin_arrival_us - neighbor_t_start_us) * 1e-6 * rate))

    a = _trim_leading_edge(origin_data, rate, origin_center)
    b = _trim_leading_edge(neighbor_data, rate, neighbor_center)

    if len(a) < _MIN_TRIM_SAMPLES or len(b) < _MIN_TRIM_SAMPLES:
        return None

    score = _score_correlation(rate, a, b, method)
    trusted = (
        score["peak_corr_coef"] >= MIN_PEAK_CORR_COEF
        and score["quality_ratio"] >= AMBIGUOUS_RATIO_THRESHOLD
    )
    return {
        "arrival_us": origin_arrival_us + score["lag_us"],
        "lag_us": score["lag_us"],
        "peak_corr_coef": score["peak_corr_coef"],
        "quality_ratio": score["quality_ratio"],
        "trusted": trusted,
    }
