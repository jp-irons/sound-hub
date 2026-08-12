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

# Fallback leading-edge trim window — only used when a caller doesn't have
# a per-species knee distance to pass in (template_pre_ms/post_ms is None —
# e.g. a tdoa_attempts row predating the 2026-07-26 window_margin_pre_ms/
# post_ms snapshot migration). These were tools/clap_sync_check.py's
# clap-tuned defaults; production callers now size the template from each
# species' own real knee distance instead (see correlate_leading_edge's
# template_pre_ms/post_ms/transit_s parameters and
# project_soundhub_tdoa_correlation_window memory) — the old fixed ~5ms
# window was found 2026-07-25 to be far narrower than real inter-node
# transit times on this property, causing confident-looking but wrong
# correlation matches. The historical "keep it narrow to dodge a struck
# object's ring-down" justification for these specific numbers doesn't
# apply to birdsong (confirmed with Jon — that reasoning traced to
# clap_sync_check.py's hand-clap tests, tapping a glass vase).
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

# Per-method trust-gate thresholds (2026-07-31, phat_masked calibration —
# see project_soundhub_phat_masked_calibrated_thresholds memory). 'plain'
# and 'gcc_phat' both keep using the module-level MIN_PEAK_CORR_COEF/
# AMBIGUOUS_RATIO_THRESHOLD above unchanged — gcc_phat has never been
# separately calibrated and no species selects it in production, so it
# inherits plain's numbers rather than being left undefined. phat_masked's
# coefficient lives on a fundamentally different scale (phase-whitened,
# not raw-amplitude) and was calibrated separately against a real
# 2530-node-pair field batch (tools/analyze_real_batch3.py): coef>=0.07,
# ratio>=2.00 is the FPR<=5% conservative operating point Jon chose from
# that sweep — recovers ~25.7% of what production's own 'plain' trust gate
# currently accepts, while agreeing with only ~5% of what it rejects.
_TRUST_THRESHOLDS: dict[str, tuple[float, float]] = {
    "plain": (MIN_PEAK_CORR_COEF, AMBIGUOUS_RATIO_THRESHOLD),
    "gcc_phat": (MIN_PEAK_CORR_COEF, AMBIGUOUS_RATIO_THRESHOLD),
    "phat_masked": (0.07, 2.00),
}


def trust_thresholds_for_method(method: str) -> tuple[float, float]:
    """(min_peak_corr_coef, min_quality_ratio) trust-gate pair for a given
    correlation_method — the same lookup correlate_leading_edge() uses
    internally below. Exposed so callers that need to *display* the
    effective thresholds (routes.py's GET /tdoa/attempts, for the Analytics
    localisation UI) read them from here rather than keeping a second copy
    of these numbers that could drift out of sync as they get retuned.
    """
    return _TRUST_THRESHOLDS.get(method, (MIN_PEAK_CORR_COEF, AMBIGUOUS_RATIO_THRESHOLD))

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


def _valid_lag_index_range(b_len: int, transit_s: float, rate: int) -> tuple[int, int]:
    """Return (valid_lo, valid_hi) — the index range in a
    correlate(a, b, mode='full') output (length len(a)+b_len-1) whose
    corresponding lag is physically achievable for this node pair.

    idx = (b_len - 1) - lag_samples (see _score_correlation's own
    peak_idx→lag_samples derivation), and the physically valid *final* lag
    (after correlate_leading_edge's own transit_s correction, lag_us =
    raw_lag_us - transit_s*1e6) is bounded to +/- transit_s around the
    primary alignment, i.e. raw lag_samples in [0, 2*transit_s*rate].
    Shared by _score_correlation (bounds which peak can be SELECTED at all
    — added 2026-07-28, see below) and _peak_quality (bounds which peak can
    be considered a COMPETITOR — added 2026-07-28, same day, earlier)."""
    transit_samples = transit_s * rate
    valid_hi = b_len - 1
    valid_lo = valid_hi - int(round(2 * transit_samples))
    return valid_lo, valid_hi


def _peak_quality(
    corr: np.ndarray, peak_idx: int, rate: int,
    transit_s: float = 0.0, b_len: int | None = None,
) -> float:
    """Ratio of the primary correlation peak to the next-highest peak found
    outside a small exclusion zone around it. Was identical to
    tools/clap_sync_check.py's _peak_quality() (minus the second_idx return,
    unused here) — diverges as of 2026-07-28 (see transit_s/b_len below);
    not backported to clap_sync_check.py, which has no widened/geometry-
    aware search window to bound against.

    transit_s/b_len (added 2026-07-28): when both are given, the competing-
    peak search is restricted to the physically valid lag range for this
    node pair (+/- transit_s around the primary alignment), not the full
    correlate(..., mode='full') output. That output spans every possible
    slide of the (narrow) origin template across the (transit_s-widened)
    neighbour buffer — most of it corresponds to lags the sound could not
    physically have travelled fast enough to produce. Real diagnostic data
    (2026-07-28, 5 field attempts / 19 node pairs) found 12 of 19 failing
    pairs' "competing" peak sat outside this bound — one case ~25x past it
    — almost certainly a partial-overlap/edge artifact or an unrelated
    repeated call elsewhere in the widened buffer, not a genuine competing
    TDOA hypothesis for this pair. Falls back to the old whole-array search
    when either arg is omitted/zero, matching correlate_leading_edge's own
    transit_s default for callers without geometry info."""
    exclude = max(1, int(round(_PEAK_EXCLUSION_MS * 1e-3 * rate)))
    masked = corr.astype(np.float64).copy()
    lo = max(0, peak_idx - exclude)
    hi = min(len(corr), peak_idx + exclude + 1)
    masked[lo:hi] = -np.inf

    if transit_s > 0.0 and b_len is not None:
        valid_lo, valid_hi = _valid_lag_index_range(b_len, transit_s, rate)
        out_of_bounds = np.ones(len(corr), dtype=bool)
        out_of_bounds[max(0, valid_lo):min(len(corr), valid_hi + 1)] = False
        masked[out_of_bounds] = -np.inf

    second_idx = int(np.argmax(masked))
    second_val = masked[second_idx]
    peak_val = corr[peak_idx]
    if not np.isfinite(second_val) or second_val <= 0:
        return float("inf")
    return float(peak_val / second_val)


def _next_pow2_fft_size(len_a: int, len_b: int) -> int:
    """Smallest power-of-two FFT size >= the full convolution length —
    shared by the gcc_phat and phat_masked branches below so both produce
    the same-length transform, and therefore the same
    [-(len(b)-1) .. +(len(a)-1)] reassembly layout plain's
    correlate(mode='full') uses."""
    n = len_a + len_b - 1
    n_fft = 1
    while n_fft < n:
        n_fft *= 2
    return n_fft


def _max_possible_peak(R: np.ndarray, n_fft: int) -> float:
    """Theoretical ceiling of |corr[n]| over every lag n, for a cross-
    correlation built by IFFT-ing the frequency-domain array R. Needed
    because plain's peak_corr_coef denominator (sqrt(a_energy*b_energy), a
    Cauchy-Schwarz bound) is only valid for raw-amplitude correlation —
    for a phase-whitened R (gcc_phat/phat_masked) it has no real ceiling
    and produces coefficients with no meaningful scale (confirmed
    2026-07-30 on real field data: gcc_phat's old raw-energy-denominator
    coefficient came out in the hundreds to thousands). By the triangle
    inequality, |corr[n]| <= (1/n_fft) * sum_k |R[k]| for every n — the
    same bound IFFT-ing |R| (real, phase-free) and evaluating at n=0
    produces, since every phase term is 1 there. Equality holds at the
    perfect-match lag, so this is a tight ceiling, not a loose one. See
    tools/validate_toa_methods.py's identically-named helper — ported
    here unchanged, kept in sync manually per this module's "copy don't
    diverge" convention (see module docstring)."""
    ceiling = np.fft.irfft(np.abs(R), n_fft)
    return float(ceiling[0])


def _band_mask(freqs: np.ndarray, low_hz: float, high_hz: float, taper_hz: float) -> np.ndarray:
    """Raised-cosine (Hann-edge) taper: ~1 inside [low_hz, high_hz],
    smoothly rolling to ~0 over taper_hz on each side. A hard rectangular
    gate would ring in the time domain and smear the very correlation peak
    this mask exists to sharpen. Ported unchanged from
    tools/validate_toa_methods.py — see that module's identically-named
    function for the full derivation."""
    lo_lo, lo_hi = max(0.0, low_hz - taper_hz), low_hz
    hi_lo, hi_hi = high_hz, high_hz + taper_hz
    mask = np.zeros_like(freqs)
    mask[(freqs >= lo_hi) & (freqs <= hi_lo)] = 1.0
    up = (freqs >= lo_lo) & (freqs < lo_hi)
    mask[up] = 0.5 * (1 - np.cos(np.pi * (freqs[up] - lo_lo) / max(lo_hi - lo_lo, 1e-9)))
    down = (freqs > hi_lo) & (freqs <= hi_hi)
    mask[down] = 0.5 * (1 + np.cos(np.pi * (freqs[down] - hi_lo) / max(hi_hi - hi_lo, 1e-9)))
    return mask


def _score_correlation(
    rate: int, a: np.ndarray, b: np.ndarray, method: str, transit_s: float = 0.0,
    band_lo: float | None = None, band_hi: float | None = None,
) -> dict:
    """Cross-correlate a against b and return lag/quality numbers.

    method is 'plain' (time-domain cross-correlation, weighted by actual
    signal energy), 'gcc_phat' (per-bin phase normalization), or
    'phat_masked' (per-bin phase normalization of the RAW cross-spectrum,
    THEN a tapered frequency-domain band mask applied to the already-
    whitened result — added 2026-07-31).
    tdoa-correlation-design-notes.md section 7 found plain clearly better
    once bandpass filtering is already applied upstream — PHAT's per-bin
    normalization re-amplifies the bandpass filter's residual stopband
    content back up to full weight, undoing much of what the filter
    bought. 'plain' is the production default (species_tdoa_params.
    correlation_method) as of 2026-07-17; 'gcc_phat' remains implemented
    since the field is now actually read rather than dead config, but no
    species currently selects it.

    band_lo/band_hi (added 2026-07-31, phat_masked only): the species'
    frequency band, applied INSIDE this function as a mask on the
    whitened spectrum, not as a pre-filter on a/b. This is load-bearing,
    not a style choice — a zero-phase bandpass filter applied to a/b
    BEFORE PHAT-style whitening cancels out almost completely at every
    bin with nonzero filter gain (the filter's squared magnitude response
    cancels exactly, top and bottom, once normalized by |R|), which is
    the proven root cause of gcc_phat's poor accuracy whenever a species
    band is configured — see project_soundhub_gcc_phat_reorder_fix /
    tools/validate_toa_methods.py's _corr_phat_masked docstring for the
    full algebra. Masking AFTER whitening, with nothing downstream to
    renormalize the suppressed bins back up, is the only ordering where
    the band restriction actually survives to the correlation peak. This
    means callers passing method='phat_masked' must pass RAW (unfiltered)
    a/b — see correlate_leading_edge's docstring. Falls back to unmasked
    full-band PHAT when band_lo/band_hi is None, matching gcc_phat's own
    no-band no-op convention. Unused for 'plain'/'gcc_phat'.

    transit_s (added 2026-07-28): bounds BOTH which peak can be selected as
    the primary match (this function, below) AND which peak can be
    considered a competitor to it (forwarded to _peak_quality() — see that
    function's docstring). The primary-peak bound was added later the same
    day, after real deployed data showed a "trusted" correlation
    (peak_corr_coef/quality_ratio both clearing their gates) with an
    implied range difference 2.96x-40.69x the physical baseline between
    the two nodes — i.e. a physically impossible lag. Root cause: only the
    *competing* peak search was ever bounded to the transit window; the
    *winning* peak itself was still chosen via argmax over the entire
    unbounded correlate(mode='full') output, so a strong, unambiguous, but
    physically-impossible match could clear both gates and still be wrong.
    Bounding the primary search too makes any correlation-selected result
    mathematically guaranteed to fall within the physical bound — the
    `consistency_ratio` diagnostic surfaced in the UI (routes.py
    _tdoa_attempt_node_record_from_row) should end up <= ~1 for every
    correlated (non-independent-fallback) node going forward. Defaults to
    0.0 (old fully-unbounded behavior) for any caller without geometry
    info, same as _peak_quality.
    """
    max_possible_peak = None
    if method == "gcc_phat":
        n_fft = _next_pow2_fft_size(len(a), len(b))
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
        max_possible_peak = _max_possible_peak(R, n_fft)
    elif method == "plain":
        corr = correlate(a, b, mode="full")
    elif method == "phat_masked":
        n_fft = _next_pow2_fft_size(len(a), len(b))
        A = np.fft.rfft(a, n_fft)
        B = np.fft.rfft(b, n_fft)
        R = A * np.conj(B)
        denom = np.abs(R)
        denom[denom == 0] = 1e-12
        R = R / denom
        if band_lo is not None and band_hi is not None:
            freqs = np.fft.rfftfreq(n_fft, d=1.0 / rate)
            taper_hz = max(0.15 * (band_hi - band_lo), 100.0)
            R = R * _band_mask(freqs, band_lo, band_hi, taper_hz)
        corr_full = np.fft.irfft(R, n_fft)
        corr = np.concatenate((corr_full[-(len(b) - 1):], corr_full[: len(a)]))
        max_possible_peak = _max_possible_peak(R, n_fft)
    else:
        raise ValueError(f"unknown correlation method '{method}'")

    # Bound the primary peak search to the physically valid lag range when
    # geometry is known (see this function's docstring, transit_s param).
    # Falls back to the full unbounded search when transit_s<=0, and again
    # if the bounded slice is somehow empty (degenerate — transit_s huge
    # relative to the buffers' own lengths, no real overlap at all): rather
    # than crash, fall back and let the downstream coef/ratio gates reject
    # whatever this finds, same failure mode as before this change existed.
    if transit_s > 0.0:
        valid_lo, valid_hi = _valid_lag_index_range(len(b), transit_s, rate)
        bound_lo = max(0, valid_lo)
        bound_hi = min(len(corr), valid_hi + 1)
        if bound_hi > bound_lo:
            peak_idx = bound_lo + int(np.argmax(corr[bound_lo:bound_hi]))
        else:
            peak_idx = int(np.argmax(corr))
    else:
        peak_idx = int(np.argmax(corr))
    # scipy.signal.correlate(a, b, mode="full")'s peak_idx - (len(b) - 1)
    # gives the shift that must be applied to A to align it onto B — i.e.
    # its sign is the OPPOSITE of "how much B is delayed relative to A".
    # Verified 2026-07-26 (project_soundhub_correlation_sign_bug memory)
    # with a minimal known-delay test: a signal genuinely arriving later in
    # b than in a produces a NEGATIVE raw value here, not positive. Negate
    # it so lag_samples/lag_us matches this function's actual contract
    # (positive means b/the neighbour is delayed relative to a/the origin)
    # and correlate_leading_edge's arrival_us = origin_arrival_us + lag_us
    # gets the correction in the right direction. This is a pre-existing
    # bug, not introduced by any per-species window sizing change — it
    # previously had little room to matter because the old fixed ~5ms
    # leading-edge window bounded how large a wrongly-signed correction
    # could ever be; the transit-aware widened window (2026-07-26) gives it
    # much more room, making this fix a prerequisite for that change being
    # safe to deploy.
    # The parabolic sub-sample refinement is computed in the ORIGINAL
    # (un-negated) scipy convention too (it refines peak_idx itself, same
    # direction), so it must be subtracted here, not added, to negate
    # consistently with the integer part above.
    lag_samples = -(peak_idx - (len(b) - 1)) - _parabolic_peak(corr, peak_idx)
    lag_us = lag_samples * 1e6 / rate

    quality_ratio = _peak_quality(corr, peak_idx, rate, transit_s=transit_s, b_len=len(b))

    # peak_corr_coef normalization is method-aware (added 2026-07-31): the
    # raw-energy Cauchy-Schwarz denominator below is only a valid [-1,1]
    # bound for 'plain' (real-amplitude) correlation. For a phase-whitened
    # corr (gcc_phat/phat_masked), corr lives on a different scale entirely
    # and dividing by raw signal energy produces coefficients with no real
    # ceiling (confirmed empirically 2026-07-30 on real field data — see
    # _max_possible_peak's docstring). max_possible_peak (set above, only
    # for gcc_phat/phat_masked) switches to the properly-bounded ceiling
    # for those two methods instead.
    if max_possible_peak is not None:
        peak_corr_coef = float(corr[peak_idx] / max_possible_peak) if max_possible_peak > 0 else 0.0
    else:
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
    template_pre_ms: float | None = None,
    template_post_ms: float | None = None,
    transit_s: float = 0.0,
    band_lo: float | None = None,
    band_hi: float | None = None,
) -> dict | None:
    """Refine a neighbour node's arrival time by cross-correlating a short
    leading-edge window of its WAV against the same real-world window of
    the origin's WAV, both anchored on origin_arrival_us — already known
    from the origin's own independent onset detection at planning time
    (routes.py _fire_cluster_after_delay).

    Pre-filtering requirement is METHOD-DEPENDENT (added 2026-07-31, see
    _score_correlation's band_lo/band_hi docstring for why): for
    method='plain'/'gcc_phat', origin_data/neighbor_data should already be
    bandpass-filtered to the species' band by the caller if one is
    configured (same filtering onset_detection.detect_onset_us applies
    before onset detection — tdoa-correlation-design-notes.md found
    bandpass helps plain correlation directly, not just onset detection).
    For method='phat_masked', origin_data/neighbor_data must instead be
    RAW/unfiltered, and band_lo/band_hi must be passed so the band
    restriction can be applied AFTER whitening, inside this function —
    pre-filtering for phat_masked would reproduce gcc_phat's proven
    filter-then-whiten bug. band_lo/band_hi are ignored for 'plain'/
    'gcc_phat'.

    template_pre_ms/post_ms (added 2026-07-26): the species' raw knee
    distance (window_margin_pre_ms/post_ms — unscaled, see
    project_soundhub_tdoa_correlation_window memory), sizing the origin's
    template exactly to the call's own real lead-in/lead-out extent instead
    of a fixed clap-tuned width. Falls back to LEADING_EDGE_PRE_MS/POST_MS
    if either is None (caller has no per-species figure to hand, e.g. an
    attempt row predating the window_margin_pre_ms/post_ms snapshot
    migration).

    transit_s: this specific origin/neighbour pair's live worst-case transit
    time (computed by the caller from node_positions — never hardcoded,
    see routes.py _correlate_attempt_node). The neighbour's search buffer is
    widened by this amount on each side of the template width, so the true
    alignment is findable anywhere within +/- transit_s of the naive
    (zero-geometry) center — fixing the historical bug where a fixed ~5ms
    window couldn't contain a true match separated by more than that (this
    property's real inter-node transit times run up to ~18ms). Defaults to
    0.0 (no widening) for callers without geometry info.

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
                         method's trust-gate thresholds (_TRUST_THRESHOLDS —
                         method-aware as of 2026-07-31; 'plain'/'gcc_phat'
                         still use the module-level MIN_PEAK_CORR_COEF/
                         AMBIGUOUS_RATIO_THRESHOLD). Numbers are returned
                         either way so callers can still log/compare a
                         distrusted result.
    """
    if origin_rate != neighbor_rate:
        g = gcd(origin_rate, neighbor_rate)
        neighbor_data = resample_poly(neighbor_data, origin_rate // g, neighbor_rate // g)
        neighbor_rate = origin_rate

    rate = origin_rate
    origin_center = int(round((origin_arrival_us - origin_t_start_us) * 1e-6 * rate))
    neighbor_center = int(round((origin_arrival_us - neighbor_t_start_us) * 1e-6 * rate))

    pre_ms = template_pre_ms if template_pre_ms is not None else LEADING_EDGE_PRE_MS
    post_ms = template_post_ms if template_post_ms is not None else LEADING_EDGE_POST_MS
    transit_ms = transit_s * 1e3

    # Origin's template is exactly knee-sized — the call's own real extent.
    # Neighbour's search buffer is widened by the live per-pair transit time
    # on each side, so the true alignment (which may sit up to transit_s
    # away from the naive zero-geometry center) is always inside the window
    # being searched, not just the window being matched against.
    a = _trim_leading_edge(origin_data, rate, origin_center, pre_ms=pre_ms, post_ms=post_ms)
    b = _trim_leading_edge(
        neighbor_data, rate, neighbor_center,
        pre_ms=pre_ms + transit_ms, post_ms=post_ms + transit_ms,
    )

    if len(a) < _MIN_TRIM_SAMPLES or len(b) < _MIN_TRIM_SAMPLES:
        return None

    score = _score_correlation(rate, a, b, method, transit_s=transit_s, band_lo=band_lo, band_hi=band_hi)

    # _score_correlation's lag measures the offset between the true content's
    # LOCAL index in a vs. in b (each relative to that buffer's own index 0
    # -- see correlation_sign_bug memory). That's only equal to the real
    # inter-node delay when both buffers' index-0 sits at the same real-time
    # instant. Widening b's PRE side by transit_ms (above) moves b's own
    # index-0 transit_ms earlier than a's, independent of any real delay --
    # a second, distinct bug from the sign one, found analytically +
    # verified empirically 2026-07-26 while re-checking the sign fix's
    # arithmetic. Must be subtracted back out here for lag_us/arrival_us to
    # mean what their docstrings say. Zero when transit_s=0.0 (the
    # backward-compatible default), so this is a no-op for any caller not
    # passing per-pair geometry.
    lag_us = score["lag_us"] - transit_s * 1e6
    min_coef, min_ratio = trust_thresholds_for_method(method)
    trusted = (
        score["peak_corr_coef"] >= min_coef
        and score["quality_ratio"] >= min_ratio
    )
    return {
        "arrival_us": origin_arrival_us + lag_us,
        "lag_us": lag_us,
        "peak_corr_coef": score["peak_corr_coef"],
        "quality_ratio": score["quality_ratio"],
        "trusted": trusted,
    }
