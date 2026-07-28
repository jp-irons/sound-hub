# TDOA correlation design notes — windowing, real-world SNR, and bandpass filtering

**Status:** Findings from synthetic feasibility testing, 2026-06-28. Informs the
hub's TDOA correlation stage; no production code has been changed as a result
of this investigation yet.

**Tooling used:** `tools/synthetic_snr_feasibility.py` — injects a clean
reference bird call into real recorded node noise at a known sub-sample delay
and controlled SNR, then runs it through the production onset-detect →
leading-edge-trim → correlate pipeline (`clap_sync_check.py`) so estimation
error can be measured against ground truth.

## 1. The original question

`clap_sync_check.py`'s leading-edge trim window (`pre_ms=1.0`, `post_ms=4.0`)
was tuned for hand-clap field tests, specifically to avoid a clap's own
acoustic ring-down dominating the correlation. The open question: does that
same narrow window work for real bird calls, which are generally fainter,
farther, and tonal/smooth rather than sharp transients?

## 2. Finding: the narrow window fails on smooth/tonal calls, independent of SNR

Tested against 7 real call recordings (Xeno-canto) covering a range of
envelope shapes:

- **Sharp-attack calls** (Torresian Crow): track correctly with the narrow
  window at any noise level.
- **Smooth/tonal calls** (Pheasant Coucal, Bush Stone-curlew, Pied
  Currawong): the narrow window fails to track the known delay even at very
  high SNR (noiseless). This is a window-design problem, not an SNR problem —
  there's no sharp transient for a 1–4ms window to anchor to.

## 3. Finding: widening the window fixes tracking at high SNR, but is much worse at realistic SNR

A noiseless sweep across `post_ms` from 4 to 300ms showed tracking becomes
reliable for all 7 calls somewhere around `post_ms ≈ 120–300ms` (Pheasant
Coucal needed the most width). Re-running with real noise injected:

| SNR     | narrow window (current default) | wide window (post_ms=300) |
|---------|----------------------------------|----------------------------|
| ≥20dB   | good for sharp calls, poor for tonal calls | excellent for all 7 calls (0–2µs error) |
| 0–10dB  | mean error 400–1100µs | mean error 50,000–160,000µs — far worse |

**Conclusion:** no single fixed window width is good across the realistic SNR
range. Widening only pays off once SNR is already high; at low/moderate SNR a
wider window gives noise far more opportunity to produce a spurious
correlation peak instead of locking onto the real call.

## 4. Finding: real in-range bird calls mostly sit at low-to-moderate SNR

To find out which SNR regime actually matters, 138 real BirdNET-confirmed
detection clips from `detections_audio/` (2026-06-11, single mic on the
property) were analyzed for in-band SNR (event RMS vs. local background RMS).

- 53% of clips have an event spanning ≥1s of the 3s chunk — continuous sound
  (insect chorus, sustained trills, wind), not a discrete arrival, so not a
  real TDOA target regardless of SNR.
- The remaining 47% are discrete sub-1s events — the population that
  actually matters for TDOA. For these: **median SNR 8.3dB, mean 9.9dB, p10
  3.0dB, p90 17.4dB. 74% fall between 0–15dB; only 2 of 65 exceed 20dB.**

This places real calls squarely in the regime where the wide window is
catastrophic and the narrow window, while much better, still has a high miss
rate (see below). **A fixed wide window is not safe to adopt for production.**

## 5. Finding: species-matched bandpass filtering recovers both miss rate and accuracy

BirdNET already supplies a species tag at detection time, so the hub (not the
node) can derive a bandpass band per species and filter both channels before
onset detection and correlation. Tested by deriving each call's band from its
own 5–95th-percentile cumulative spectral energy (±150Hz margin), 4th-order
Butterworth, applied to the full noisy buffer before the rest of the
pipeline. Re-ran the SNR=0/5/10/15dB sweep (the realistic band from §4),
narrow vs. wide window, with vs. without the filter, 7 calls × 15 trials ×
3 delays per condition:

| config            | miss rate (SNR 0–15dB) | mean lag error |
|--------------------|--------------------------|------------------|
| narrow, no filter  | 71–78%                   | 500–1100µs       |
| **narrow, filtered** | **10–11%**              | **200–300µs** (median 1–10µs for most calls) |
| wide, no filter    | 71–78%                   | 23,000–153,000µs |
| wide, filtered     | 10–11%                   | 3,500–15,000µs (still far worse than narrow+filtered) |

Bandpass + narrow window is the strongest combination found: for 6 of 7 calls
tested (both Bush Stone-curlew clips, both Pied Currawong clips, both
Torresian Crow clips), median lag error lands at 1–344µs even at SNR=0dB.
Filtering does **not** rescue the wide window — confirms its problem is
structural (extra noise surface area for spurious correlation peaks), not an
SNR effect that filtering can compensate for.

**Architectural implication:** since species ID only exists after BirdNET has
run, species-matched bandpass filtering is naturally a hub-side step, not a
node-side one. Suggested hub pipeline: pull raw segments from nodes → BirdNET
tags species → hub derives/looks up that species' band → bandpass both
channels → narrow leading-edge trim (keep current `pre_ms=1.0`/`post_ms=4.0`
defaults — do not widen) → correlate.

## 6. Open exception: low-frequency, short, smooth calls (e.g. Pheasant Coucal)

Even with its own matched band (50–180Hz), Pheasant Coucal still missed
138/180 trials (vs. 0/180 for every other call tested). It's the shortest
(0.2s), smoothest, and lowest-frequency call in the set, and outdoor
low-frequency noise (wind, rumble) isn't cleanly separable from it at that
band with a simple bandpass. This class of call likely needs to be flagged as
low-confidence-for-TDOA rather than forced through the same pipeline, or
needs a different trigger/correlation strategy (e.g. matched filtering against
a template rather than energy-onset + leading-edge correlation).

**Every recommendation in this note is individually wrong for Coucal,
specifically.** Breaking it out:

- *Bandpass doesn't reduce its miss rate* (stays 71–80% with or without
  filtering, vs. ~75%→~10% for the other six calls), and **gets worse as SNR
  rises** instead of better — unfiltered, miss rate properly drops to 0/45 by
  SNR=15 and mean error falls from 1132µs to 562µs; filtered, miss rate
  *climbs* to 36/45 and mean error climbs too. Likely cause: the 50–180Hz
  band is narrow and low relative to the call's already-short duration
  (0.2s), and narrow frequency localization costs time localization — the
  filter smears the call's energy in time, blunting the sharp-ish rise
  `detect_onset()` needs.
- *The method ranking flips.* For the other six calls plain correlation was
  clearly best. For Coucal, plain-PHAT (unmasked) is best (mean 881–1147µs),
  plain correlation is mid (1131–1534µs), and masked-PHAT is worst
  (1384–2346µs) — the reverse order.
- Best result found for Coucal across every config tested is the
  **unfiltered narrow window** (mean error 562µs by SNR=15, miss rate 0/45 at
  SNR=15 but 18–35/45 below SNR=10) — still the hardest species by a wide
  margin, and not fixable by tuning any of the levers explored so far.

**Untested candidate: adaptive lattice filter (ALE) for correlation cleaning,
not onset detection.** The noise that defeats Coucal's bandpass is described
above as low-frequency wind/rumble overlapping its 50–180Hz band — broadband,
not tonal. A fixed-frequency filter can't separate "in-band but not the call"
from "in-band and is the call"; an adaptive line enhancer separates by
*predictability* instead (locks onto narrowband/dominant content, leaves
broadband/non-stationary content in the residual), which is a structurally
better match to that specific noise profile. Two important scoping notes:
this would replace the bandpass step ahead of correlation, not the onset
detector — Coucal's 0.2s call is too short for an ALE to converge before the
call ends, so onset detection should stay on the unfiltered narrow window as
above — and it would only make sense paired with a widened correlation window
(§8) where there's actual time for the filter to adapt. Known risk (see
[[project-bird-lattice-ale-idea]] in memory): a single-stage ALE locks onto
whichever signal is loudest and most tonal in the buffer, so a louder cicada
chorus in the same window could hijack it instead of the bird call; the
cascaded-deflation extension is the proposed fix, also untested. Nothing has
been built or benchmarked for this yet — flagged here as the most promising
next experiment for Coucal specifically, not a tested result.

**Untested idea: frequency-domain (FM/chirp) correlation as a different anchor
than amplitude.** Every method evaluated in this note — onset detection,
leading-edge trim, bandpass, plain correlation, GCC-PHAT — operates in the
amplitude domain: each ultimately needs an amplitude feature (a transient
edge, or at minimum stable in-band energy) to anchor onto. The likely reason
Coucal resists all of them is exactly that it has no sharp transient, so
there's nothing in amplitude for a window to lock onto, and narrowing the
band to isolate it in frequency only smears its already-short time
localization further (see above).

Raised 2026-06-28, not yet checked against a real Coucal spectrogram: if the
call's pitch sweeps *within* the note rather than staying flat, the usable
anchor may be the instantaneous-frequency trajectory, not the amplitude
envelope — a dimension none of the methods above inspect. A
frequency-modulated signal has fundamentally different correlation behavior
than a constant tone or a transient: shifting a chirp against a copy of
itself by anything other than the true delay misaligns the swept frequencies
and the correlation falls off sharply away from the true lag (the
pulse-compression principle used in radar/sonar) — a sharp, unambiguous peak
that needs no sharp amplitude onset to produce.

This would require a frequency-*tracking* filter (not a converge-and-hold
ALE/PLL, since the target frequency is moving) to follow the sweep closely
enough to act as a real-time, call-specific matched filter. Two untested
candidate correlation targets:

1. Cross-correlate the tracked/predicted waveform output between nodes
   (cleaner than raw or bandpassed audio).
2. Cross-correlate the recovered instantaneous-frequency trajectory itself
   between nodes — amplitude-independent (useful since two nodes at
   different range see very different received levels for the same call)
   and a compact 1-D curve rather than a full audio buffer.

Open empirical question before any of this is worth building: is the pitch
variation within a single note (helps a single ~0.2s correlation window), or
is it the descending pitch across the *sequence* of notes in the song, with
each individual note closer to a flat tone (a much longer-timescale pattern
that wouldn't help a short correlation window at all)? Needs a look at an
actual spectrogram. Fuller reasoning and caveats (tracking-rate-vs-lag
tradeoff, common-mode lag cancellation across nodes) logged in the
[[project-bird-lattice-ale-idea]] memory note.

## 7. Finding: GCC-PHAT does not benefit from bandpass filtering — plain correlation remains the winner

Hypothesis going in: PHAT normalizes every frequency bin to unit magnitude
before correlating, so unlike plain correlation it has no built-in defense
against out-of-band noise — bandpass filtering should help PHAT *more* than
plain correlation, by removing that noise before PHAT ever flattens it to
equal weight. Tested directly (narrow window, with/without bandpass, both
methods scored on identical trials, all 7 calls, SNR 0–15dB):

| config              | method | mean error | median error |
|---------------------|--------|------------|---------------|
| narrow, no filter    | plain  | 513–1132µs | 344–628µs |
| narrow, no filter    | phat   | 750–1502µs | 400–1374µs |
| narrow, **filtered** | plain  | **202–293µs** | **3.3–3.9µs** (SNR≥5dB) |
| narrow, **filtered** | phat   | 407–696µs  | 371–398µs |

The hypothesis was wrong. Without filtering, plain and PHAT perform
similarly (no clear winner). With filtering, plain correlation pulls far
ahead and PHAT barely improves over its unfiltered baseline.

**Why:** a bandpass filter's stopband isn't perfectly zero, just heavily
attenuated. Plain correlation weights by actual signal energy, so that
already-tiny residual stopband content stays negligible. PHAT's per-bin
normalization re-amplifies that same tiny residual back up to full unit
weight, alongside the real in-band signal — undoing much of what the filter
was supposed to buy. Filtering and PHAT's normalization work against each
other rather than together.

**Conclusion: use plain correlation, not GCC-PHAT, in combination with
species-matched bandpass filtering.**

**Follow-up — does hard-zeroing the stopband fix PHAT's problem?** The IIR
filter above attenuates the stopband but doesn't zero it exactly, and PHAT's
per-bin normalization re-amplifies whatever's left back to full unit weight.
Tested a masked-PHAT variant that hard-zeros the FFT bins outside the
species' band directly inside the PHAT computation (so those bins are
exactly 0/0→0 under normalization, rather than residual-noise/residual-noise
→1):

| method                          | mean error (SNR 0–15dB) | median error |
|----------------------------------|---------------------------|----------------|
| plain + bandpass                 | 202–293µs                  | **3.3–3.9µs**   |
| phat + bandpass (IIR filter only) | 407–696µs                  | 370–398µs       |
| phat + hard spectral mask         | 238–340µs                  | 27–116µs        |

This confirms the mechanism: hard-zeroing roughly halves PHAT's mean error
and cuts its median by ~10x versus relying on the IIR filter's attenuation
alone. It does not change the overall ranking, though — plain correlation +
bandpass is still ahead by roughly an order of magnitude on median error.
Per-species the picture is mixed: masked-PHAT edges out plain for both
Torresian Crow clips and ties on one Pied Currawong clip, but is
substantially worse on Pheasant Coucal (1911µs vs. 1259µs) — its very narrow
50–180Hz band seems to lose too much information under hard masking.
Recommendation is unchanged: plain correlation + bandpass.

## 8. Finding: wide window + bandpass has an excellent median but a heavy tail — narrow window remains the safer default

§5 reported wide+filtered as still "far worse than narrow+filtered" based on mean error. Re-tested wide window
(`pre_ms=75`, `post_ms=300`) + bandpass with plain, PHAT, and masked-PHAT, and looked at median (not just mean)
to separate typical-case from worst-case behaviour:

| method                    | median error (SNR≥5dB), 6 non-Coucal calls | mean error | p90 (SNR=0dB) |
|---------------------------|-----------------------------------------------|------------|-----------------|
| plain + wide + bandpass        | **0.1µs**                                  | 0.1–6,658µs | up to 65,900µs |
| phat (unmasked) + wide + bandpass | 370–400µs                               | 360–32,500µs | up to 18,900µs |
| phat (masked) + wide + bandpass   | 0.2–3.8µs                               | 2.3–5,317µs | up to 29,100µs |

The mean was dominated by a small fraction of trials producing a spurious correlation peak far from the true
delay — the median tells a very different and much more favourable story: **for 6 of 7 calls, wide+bandpass+plain
correlation is typically more accurate than narrow+bandpass+plain** (median 0.1µs vs. narrow's 3.3–3.9µs).
Masked-PHAT is a close second and occasionally ties or edges out plain per-species. Unmasked PHAT remains clearly
worse, consistent with §7.

The catch is tail risk: narrow+bandpass's worst case (p90) tops out around 500–600µs; wide+bandpass's worst case
can be tens of thousands of µs. **Recommendation: do not switch to wide+bandpass as the production default without
also implementing a quality-gating step** (e.g. reject estimates below a `peak_corr_coef` threshold, falling back
to the narrow-window estimate or flagging low-confidence). Untested. Until that gating exists, narrow+bandpass
remains the safer choice despite its slightly worse typical-case accuracy, because it has no comparably severe
failure mode.

Pheasant Coucal is unaffected by this — wide window is already catastrophic for it (§3) and bandpass does not
rescue it in this configuration either (138/180 missed, median error >100,000µs across all three methods when
detected at all). No change to its status as a separate, unsolved problem (§6).

## 9. Not yet tested / open items

- Per-species bands were derived from a single clean reference recording each,
  not a robust library — real call-to-call variation within a species (and
  the effect of getting the band slightly wrong) hasn't been characterized.
- The bandpass-derived band was tested as a fixed filter; an adaptive or
  confidence-weighted approach (e.g. falling back to a wider band if the
  narrow one yields no correlation peak above quality threshold) hasn't been
  explored.
- All testing used noise drawn from two recording runs from one node
  (MAC `_97230`); broader noise diversity (wind days, rain, different times of
  day) hasn't been tested.
- A quality-gating step (e.g. `peak_corr_coef` thresholding to reject/flag bad
  correlation estimates) is now built for the narrow-window production path
  (`server/correlation.py`'s `MIN_PEAK_CORR_COEF`/`AMBIGUOUS_RATIO_THRESHOLD`,
  2026-07-17 — same threshold values validated here and in
  `clap_sync_check.py` against real hand-clap tests, not independently
  re-derived for bird calls). Still not built or tested for wide+bandpass
  specifically — §8's recommendation ("do not switch to wide+bandpass
  without also implementing a quality-gating step") is therefore still
  open; the gate that exists guards the narrow-window path only.

## 10. Recommended next steps for hub TDOA implementation

1. Build a per-species band lookup (derive once per target species from clean
   reference recordings, store in the hub). **Done 2026-07-13** for the 4
   currently-configured species (Gray Butcherbird, Noisy Miner, Pied
   Currawong, Torresian Crow) — see `config/species_tdoa_params.json` /
   `tools/README.md`.
2. Implement the bandpass step in the hub's correlation pipeline, ahead of
   the existing leading-edge trim + correlation, keeping the leading-edge
   window at its current narrow (clap-tuned) defaults, scored with plain
   cross-correlation (not GCC-PHAT — see §7). **Done 2026-07-17, not yet
   deployed** — `server/correlation.py` (leading-edge trim + scoring,
   confidence gate) wired into `routes.py`'s `_correlate_attempt_node`;
   bandpass applied to both channels ahead of correlation when a species has
   a configured band; `correlation_method` defaulted to `plain` for all 4
   species per this section's finding. See `project_bird_tdoa_correlation_gap`
   memory for the full implementation writeup — production's version differs
   from this doc's tooling in one structural way worth knowing: the origin
   and neighbour WAVs don't share a common sample-index timebase (the origin
   is a raw trigger capture, not pulled against the shared window), so each
   buffer's leading-edge trim center is computed independently from its own
   `t_start_us` against the shared `origin_arrival_us` anchor, rather than
   trimming both buffers around one shared index the way
   `clap_sync_check.py`'s `trim_to_leading_edge()` does.
3. Decide how to handle species known to fail this approach (Coucal-class
   calls): exclude from auto-localization, surface a low-confidence flag, or
   build a separate matched-filter path. **Still open** — none of the 4
   currently-configured species are Coucal-class, so not yet urgent, but
   unresolved if/when one is added.
4. Broaden the noise corpus used for validation (currently two recording runs
   from one node). **Still open.**

## 11. Follow-up (2026-07-28): the ambiguity gate's secondary-peak search wasn't bounded to the physical window

§9's quality-gating step (`MIN_PEAK_CORR_COEF`/`AMBIGUOUS_RATIO_THRESHOLD`)
was built and deployed for the narrow-window production path, but a real
bug in how it picked the "competing" peak wasn't caught until real field
data forced the question. `_peak_quality()` searched the *entire*
`scipy.signal.correlate(..., mode='full')` output for a secondary peak —
not just the physically valid lag range the transit-aware widened window
(§8's `transit_s` widening, later made production in
`project_soundhub_tdoa_correlation_window`) was meant to represent. Real
diagnostic data (5 attempts, 19 node pairs, Pied Currawong config): 12 of
19 pairs had their ambiguity-suppressing "competitor" sitting outside the
pair's own max-transit bound (`distance / 343`), in one case ~25x past it.
Bounding that search to `± transit_s` recovered most of them (trusted
correlations 4/19 → 14/19). The remaining ~5 pairs have a genuine
in-bound competing peak — real call repetition/periodicity, not a search
artifact — and are the leading candidate for §8's still-open "quality-
gating for wide windows" recommendation, this time via a narrower
correlation *template* rather than a wider one. See
`project_soundhub_correlation_ambiguity_bound` memory for the full
diagnostic and fix, and `project_soundhub_timestamp_precision_bug` for a
separate, upstream bug (in `tdoa_solver.py`, not this module) that was
very likely a bigger contributor to historically "implausible" solves than
anything in this document.
