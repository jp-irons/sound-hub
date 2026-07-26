"""
Regression test for the 2026-07-26 _score_correlation sign bug (see
project_soundhub_correlation_sign_bug memory / correlation.py's comment
above the lag_samples negation).

_score_correlation's raw scipy.signal.correlate(a, b, mode="full") lag
formula (peak_idx - (len(b) - 1)) gives the shift needed to align A onto B,
which is the OPPOSITE sign of "how far B is delayed relative to A" —
correlate_leading_edge's arrival_us = origin_arrival_us + lag_us contract
needs the latter. This was a real, pre-existing bug (not introduced by any
per-species window sizing work), verified with the minimal known-delay
construction below before being fixed. Kept here as a permanent regression
test so this can't silently reappear -- discovered only because a synthetic
ground-truth validation experiment (tools/validate_deramp_correlation.py)
happened to be built for an unrelated question (whether de-ramping the
leading-edge window helps) and exposed it.
"""

import numpy as np
import pytest

from server.correlation import _score_correlation, correlate_leading_edge

RATE = 1000
N = 400


def _make_signal() -> np.ndarray:
    x = np.zeros(N)
    x[200:220] = np.linspace(0, 1, 20)
    x[220:240] = np.linspace(1, 0, 20)
    return x


@pytest.mark.parametrize("delay_samples", [15, -15, 0, 7, -22])
def test_score_correlation_lag_sign(delay_samples):
    """A neighbour buffer (b) whose content genuinely arrives
    delay_samples LATER than the origin buffer's (a) copy of the same
    content must produce lag_us with the SAME sign as delay_samples —
    positive means b/the neighbour is delayed relative to a/the origin,
    matching correlate_leading_edge's arrival_us = origin_arrival_us +
    lag_us contract."""
    x = _make_signal()
    a = x.copy()
    b = np.zeros(N)
    if delay_samples >= 0:
        b[delay_samples:] = x[: N - delay_samples]
    else:
        b[: N + delay_samples] = x[-delay_samples:]

    score = _score_correlation(RATE, a, b, "plain")
    expected_us = delay_samples * 1e6 / RATE
    assert score["lag_us"] == pytest.approx(expected_us, abs=1.0)


@pytest.mark.parametrize(
    "transit_s, true_delay_samples",
    [
        # transit_s must cover |true_delay| for the true content to even be
        # inside the searched window -- these are all physically valid
        # (transit_s in seconds, true_delay_samples at rate=48000).
        (0.0, 0),        # backward-compatible default: no widening, no delay
        (0.005, 96),     # 5ms widening comfortably covers a 2ms delay
        (0.005, -96),
        (0.005, 0),
        (0.018, 96),     # this property's real worst-case transit (~18ms)
        (0.018, -96),
    ],
)
def test_correlate_leading_edge_arrival_direction(transit_s, true_delay_samples):
    """End-to-end check at the correlate_leading_edge level (not just
    _score_correlation directly): a neighbour whose true arrival differs
    from the origin's by a known amount must produce arrival_us offset by
    that same amount, regardless of transit_s (the neighbour search-buffer
    widening added 2026-07-26). transit_s > 0 exercises a second, distinct
    bug found the same day: widening the neighbour's buffer asymmetrically
    (extra samples on the PRE side only) shifts its own local-index-0
    reference point earlier than the origin template's by exactly
    transit_ms, independent of any real delay -- that constant offset was
    leaking straight into lag_us/arrival_us until correlate_leading_edge
    started subtracting it back out. transit_s=0.0 covers the
    backward-compatible default (no widening, offset should be a no-op).

    Uses a realistic sample rate and a sharp, distinctive click (rather
    than test_score_correlation_lag_sign's coarse tent shape) so the
    correlation peak is unambiguous."""
    rate = 48000
    n = 9600  # 200ms buffer -- room for the largest transit_s tested
    onset_idx = 4800  # comfortably clear of both edges
    x = np.zeros(n)
    click_len = 48  # 1ms sharp attack/decay
    x[onset_idx:onset_idx + click_len // 2] = np.linspace(0, 1, click_len // 2)
    x[onset_idx + click_len // 2:onset_idx + click_len] = np.linspace(1, 0, click_len // 2)

    t_start_us = 0.0
    origin_arrival_us = onset_idx / rate * 1e6

    origin_data = x.copy()
    neighbor_data = np.zeros(n)
    if true_delay_samples >= 0:
        neighbor_data[true_delay_samples:] = x[: n - true_delay_samples]
    else:
        neighbor_data[: n + true_delay_samples] = x[-true_delay_samples:]

    result = correlate_leading_edge(
        origin_data=origin_data, origin_rate=rate, origin_t_start_us=t_start_us,
        origin_arrival_us=origin_arrival_us,
        neighbor_data=neighbor_data, neighbor_rate=rate, neighbor_t_start_us=t_start_us,
        method="plain",
        template_pre_ms=1.0, template_post_ms=1.0, transit_s=transit_s,
    )
    assert result is not None
    expected_arrival_us = origin_arrival_us + true_delay_samples * 1e6 / rate
    assert result["arrival_us"] == pytest.approx(expected_arrival_us, abs=50.0)
