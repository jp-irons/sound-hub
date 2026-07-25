"""
derive_species_bands.py — Derive per-species TDOA bandpass bands from
Xeno-canto reference recordings in test/samples/.

Why this exists:
    species_tdoa_params.freq_band_low_hz/freq_band_high_hz (see
    docs/tdoa-correlation-design-notes.md, tools/README.md's "Species TDOA
    parameter portability" section) were, up to 2026-07-13, derived by hand:
    manually picking individual calls out of a reference recording, running
    synthetic_snr_feasibility.py's derive_band() on each pick, and
    eyeballing the results into config/species_tdoa_params.json. That
    doesn't scale to reprocessing 8 species across ~60 source files. This
    script automates the "pick calls out of a longer recording" step with an
    energy-envelope segmenter, then runs the same derive_band() (imported,
    not reimplemented — see synthetic_snr_feasibility.py) on every isolated
    call.

    Deliberately report-only: this script never writes
    config/species_tdoa_params.json. It prints a full per-file/per-call
    table plus a proposed-vs-current diff for species that already have a
    band, so a human reviews the numbers (and any flagged outliers/dupes)
    before they're transcribed in — same review gate the manual process
    had, just applied to automated output instead of an ad-hoc eyeball pass.

Pipeline per species:
    1. Group test/samples/<species>/* by Xeno-canto catalog number (the
       leading "XCnnnnnn" token). Where a catalog number has more than one
       file (seen in practice as "NAME.mp3" + "NAME (1).mp3" pairs), compare
       file bytes: identical -> drop the duplicate silently (logged);
       different -> keep both and flag for manual review.
    2. Convert mp3 -> 48kHz mono wav via ffmpeg (same parameters as
       mp3_to_wav.bat) into a scratch dir; existing .wav files are used
       as-is. Load every kept file once up front.
    3. Cross-catalog content-duplicate check: some Xeno-canto recordings get
       re-listed under a different catalog number entirely (seen in
       practice: Gray Butcherbird's XC640428 is the same underlying
       recording as XC635936 — already noted by hand in
       config/species_tdoa_params.json). Same-catalog byte-hashing above
       can't catch this since the catalog numbers differ. Instead, compare
       every pair of loaded files in the species by duration and a coarse
       (~50Hz) normalized energy-envelope fingerprint; a near-identical
       duration plus a very high fingerprint correlation flags the pair as
       likely-duplicate content, and the later (lexicographically second)
       file is dropped.
    4. Segment each recording into candidate call events via a short-time
       energy envelope: threshold at a multiple of a low-percentile
       background estimate (not the median -- these clips are often
       call-dominant, unlike a quiet field recording, so the median would
       already include call energy), merge segments separated by a short
       gap, drop segments shorter than a minimum duration, pad what's left.
    5. Run derive_band() on each isolated segment. Segments whose derived
       low edge lands at/near derive_band()'s own 50Hz floor are dropped as
       "no defined tonal content" (broadband noise, wind, silence tail)
       rather than counted as a call — found necessary empirically: an
       earlier uncalibrated pass on Gray Butcherbird pulled ~24% of
       "calls" straight off this floor, dragging the proposed band well
       below the previously hand-validated 900-3000 Hz value.
    6. Aggregate per species: median low/high across all remaining calls,
       with a robust (MAD-based) outlier flag on either edge -- flagged
       calls are reported but excluded from the "clean" median, mirroring
       how XC580877 was manually excluded from Pied Currawong's original
       derivation as a likely wind/rustle-contaminated outlier.

Usage:
    python tools/derive_species_bands.py
    python tools/derive_species_bands.py --species "Gray Butcherbird,Noisy Miner"
    python tools/derive_species_bands.py --json-out report.json

Requires: numpy, scipy, ffmpeg on PATH. Run from the sound-hub root
directory (same requirement as synthetic_snr_feasibility.py, for the
clap_sync_check import).
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

import numpy as np

from clap_sync_check import _load_mono, _energy_envelope
from synthetic_snr_feasibility import derive_band, _resample_if_needed


TARGET_RATE = 48000
SAMPLES_DIR = os.path.join(os.path.dirname(__file__), "..", "test", "samples")
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "species_tdoa_params.json")

# Pheasant Coucal's call is low-frequency with no sharp transient (flagged
# in docs/tdoa-correlation-design-notes.md as a poor candidate for this
# pipeline); left in test/samples/ but skipped here per 2026-07-25 direction.
DEFAULT_EXCLUDE = {"Pheasant Coucal"}

_CATALOG_RE = re.compile(r"^(XC\d+)")


def parse_catalog(filename: str) -> str:
    m = _CATALOG_RE.match(filename)
    return m.group(1) if m else filename


def dedupe_by_catalog(files: list) -> tuple:
    """Group by catalog number; within a group, drop byte-identical
    duplicates. Returns (kept_paths, log_lines).
    """
    groups = {}
    for path in files:
        groups.setdefault(parse_catalog(os.path.basename(path)), []).append(path)

    kept = []
    log = []
    for catalog, paths in sorted(groups.items()):
        if len(paths) == 1:
            kept.append(paths[0])
            continue
        hashes = {}
        for path in paths:
            with open(path, "rb") as f:
                h = hashlib.sha256(f.read()).hexdigest()
            hashes.setdefault(h, []).append(path)
        if len(hashes) == 1:
            survivor = sorted(paths)[0]
            kept.append(survivor)
            for path in sorted(paths):
                if path != survivor:
                    log.append(f"  [dedup]  {catalog}: dropped byte-identical duplicate "
                                f"{os.path.basename(path)} (kept {os.path.basename(survivor)})")
        else:
            kept.extend(paths)
            names = ", ".join(os.path.basename(p) for p in sorted(paths))
            log.append(f"  [FLAG]   {catalog}: {len(paths)} files share this catalog number "
                        f"but differ in content -- kept all ({names}); review manually.")
    return kept, log


def ensure_wav(path: str, tmp_dir: str) -> str:
    if path.lower().endswith(".wav"):
        return path
    os.makedirs(tmp_dir, exist_ok=True)
    out_name = os.path.splitext(os.path.basename(path))[0] + ".wav"
    out_path = os.path.join(tmp_dir, out_name)
    if os.path.exists(out_path):
        return out_path
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-ar", str(TARGET_RATE), "-ac", "1", out_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"ffmpeg conversion failed for {path}: {result.stderr.strip()}")
    return out_path


def load_all(paths: list, tmp_dir: str) -> dict:
    """Load every file once. Returns {path: (rate, data, duration_s)}, all
    resampled to TARGET_RATE. Files that fail to decode are omitted (logged
    by the caller via the missing key).
    """
    loaded = {}
    for path in paths:
        wav_path = ensure_wav(path, tmp_dir)
        rate, data = _load_mono(wav_path)
        data = _resample_if_needed(rate, data, TARGET_RATE)
        loaded[path] = (TARGET_RATE, data, len(data) / TARGET_RATE)
    return loaded


def _fingerprint(data: np.ndarray, rate: int, fp_rate: float = 50.0) -> np.ndarray:
    """Coarse (~fp_rate Hz) normalized energy-envelope fingerprint, used only
    for cross-catalog duplicate-content detection -- not for call
    segmentation (see segment_calls for that, which needs finer resolution).
    """
    env = _energy_envelope(data, rate, window_ms=20.0)
    block = max(1, int(round(rate / fp_rate)))
    n_blocks = len(env) // block
    if n_blocks < 2:
        return env
    fp = env[: n_blocks * block].reshape(n_blocks, block).mean(axis=1)
    fp = fp - fp.mean()
    std = fp.std()
    return fp / std if std > 0 else fp


def _fingerprint_similarity(fp_a: np.ndarray, fp_b: np.ndarray, max_lag: int = 100) -> float:
    """Best normalized cross-correlation between two fingerprints within
    +/- max_lag fingerprint-samples (2s at the default 50Hz fingerprint
    rate). 1.0 = perfect match.
    """
    norm = np.sqrt(np.sum(fp_a.astype(np.float64) ** 2) * np.sum(fp_b.astype(np.float64) ** 2))
    if norm == 0:
        return 0.0
    corr = np.correlate(fp_a, fp_b, mode="full") / norm
    center = len(fp_b) - 1
    lo, hi = max(0, center - max_lag), min(len(corr), center + max_lag + 1)
    if hi <= lo:
        return 0.0
    return float(np.max(corr[lo:hi]))

# Note: keep threshold conservative -- two genuinely different recordings
# of the same species essentially never align this well on a coarse
# amplitude-envelope fingerprint.
_DUPLICATE_FINGERPRINT_THRESHOLD = 0.97
_DUPLICATE_DURATION_TOL_FRAC = 0.02  # 2%
_DUPLICATE_DURATION_TOL_MIN_S = 0.5


def dedupe_by_content(loaded: dict) -> tuple:
    """Pairwise cross-catalog duplicate-content check across every loaded
    file in a species (regardless of catalog number). Returns
    (kept_paths, log_lines); on a flagged pair, keeps the
    lexicographically-first path, drops the other.
    """
    paths = sorted(loaded.keys())
    dropped = set()
    log = []
    for i in range(len(paths)):
        if paths[i] in dropped:
            continue
        rate_a, data_a, dur_a = loaded[paths[i]]
        for j in range(i + 1, len(paths)):
            if paths[j] in dropped:
                continue
            rate_b, data_b, dur_b = loaded[paths[j]]
            tol = max(_DUPLICATE_DURATION_TOL_MIN_S, _DUPLICATE_DURATION_TOL_FRAC * min(dur_a, dur_b))
            if abs(dur_a - dur_b) > tol:
                continue
            sim = _fingerprint_similarity(_fingerprint(data_a, rate_a), _fingerprint(data_b, rate_b))
            if sim >= _DUPLICATE_FINGERPRINT_THRESHOLD:
                dropped.add(paths[j])
                log.append(f"  [dedup]  content-duplicate: dropped {os.path.basename(paths[j])} "
                            f"(kept {os.path.basename(paths[i])}) -- duration diff {abs(dur_a - dur_b):.2f}s, "
                            f"fingerprint similarity {sim:.3f}")
    kept = [p for p in paths if p not in dropped]
    return kept, log


def segment_calls(data: np.ndarray, rate: int, window_ms: float = 20.0,
                   threshold_factor: float = 6.0, background_pct: float = 20.0,
                   min_duration_ms: float = 80.0, merge_gap_ms: float = 150.0,
                   pad_ms: float = 30.0) -> list:
    """Return a list of (start_idx, end_idx) sample-index pairs marking
    candidate call events in a (possibly multi-call, multi-second) recording.

    background_pct uses a low percentile of the energy envelope rather than
    the median as the "quiet" reference -- these are Xeno-canto species
    recordings, often call-dominant rather than mostly-silent, so the median
    can already sit inside call energy and produce a threshold too high to
    find anything. threshold_factor defaults higher than an earlier,
    uncalibrated pass (4.0) used -- that pass picked up faint background/
    insect noise as "calls" on ~24% of segments (see module docstring,
    step 5's floor-filter note); still deliberately lower than
    clap_sync_check's clap-tuned 8.0x sanity check, since this is segmenting
    continuous bird call phrases, not picking one sharp transient out of
    near-silence.
    """
    env = _energy_envelope(data, rate, window_ms)
    background = np.percentile(env, background_pct)
    threshold = max(background * threshold_factor, 1e-9)

    above = env > threshold
    n = len(above)
    merge_gap = max(1, int(round(merge_gap_ms * 1e-3 * rate)))
    min_duration = max(1, int(round(min_duration_ms * 1e-3 * rate)))
    pad = max(0, int(round(pad_ms * 1e-3 * rate)))

    runs = []
    i = 0
    while i < n:
        if above[i]:
            j = i
            while j < n and above[j]:
                j += 1
            runs.append([i, j])
            i = j
        else:
            i += 1

    merged = []
    for run in runs:
        if merged and run[0] - merged[-1][1] <= merge_gap:
            merged[-1][1] = run[1]
        else:
            merged.append(run)

    segments = []
    for start, end in merged:
        if end - start < min_duration:
            continue
        segments.append((max(0, start - pad), min(n, end + pad)))
    return segments


def derive_bands_for_file(path: str, rate: int, data: np.ndarray, margin_hz: float,
                           min_low_hz: float, seg_kwargs: dict) -> tuple:
    """Segment one already-loaded recording and derive a band per accepted
    call. Returns (rows, warnings).
    """
    segments = segment_calls(data, rate, **seg_kwargs)

    warnings = []
    if not segments:
        warnings.append(f"    [warn] no candidate calls found -- recording may be too quiet, "
                         f"or --threshold-factor is too high for it.")
    elif len(segments) > 30:
        warnings.append(f"    [warn] {len(segments)} candidate segments found -- unusually many; "
                         f"this file may be noisy/busy rather than cleanly separated calls. "
                         f"Consider a higher --threshold-factor for this file.")

    rows = []
    dropped_floor = 0
    for idx, (start, end) in enumerate(segments):
        clip = data[start:end]
        try:
            low_hz, high_hz = derive_band(clip, rate, margin_hz=margin_hz)
        except ValueError as e:
            warnings.append(f"    [warn] segment {idx} ({start/rate:.2f}-{end/rate:.2f}s): {e}")
            continue
        if low_hz <= min_low_hz:
            # Derived lower edge at/near derive_band()'s own 50Hz floor --
            # no defined tonal content, i.e. this segment is broadband noise
            # / wind / a silence tail the threshold let through, not a call.
            dropped_floor += 1
            continue
        rows.append({
            "file": os.path.basename(path),
            "start_s": start / rate,
            "end_s": end / rate,
            "duration_s": (end - start) / rate,
            "low_hz": low_hz,
            "high_hz": high_hz,
        })
    if dropped_floor:
        warnings.append(f"    [warn] {dropped_floor} segment(s) dropped as no-tonal-content "
                         f"(derived low edge <= {min_low_hz:.0f} Hz)")
    return rows, warnings


def mad_outlier_mask(values: np.ndarray, z_thresh: float) -> np.ndarray:
    """Purely informational flag now (see aggregate_species) -- not used to
    compute the proposed band. Kept to call out individual calls wild
    enough to be worth a manual look (e.g. a likely wind/rustle-
    contaminated segment), separate from the percentile trimming that
    already bounds the proposed range itself.
    """
    median = np.median(values)
    mad = np.median(np.abs(values - median))
    if mad == 0:
        return np.zeros(len(values), dtype=bool)
    z = 0.6745 * (values - median) / mad
    return np.abs(z) > z_thresh


def aggregate_species(rows: list, outlier_z: float, low_pct: float, high_pct: float) -> dict:
    """Proposed band = [low_pct percentile of low_hz, high_pct percentile of
    high_hz] across all accepted calls.

    Replaces an earlier attempt (2026-07-25) at auto-detecting discrete call
    "clusters" via a gap in sorted high_hz -- that looked promising on a
    coarse percentile table (Gray Butcherbird: 73%/27% split at a ~3000Hz
    cutoff) but didn't hold up once the actual sorted spectrum was
    inspected: it's a smooth, continuous, right-skewed spread from ~950Hz
    to ~11600Hz with no real gap anywhere (largest ratio between adjacent
    sorted values was ~1.3x, nowhere near a genuine split). Treating that as
    two clusters would have been fabricating structure the data doesn't
    have -- more likely explanation is that individual energy-threshold
    segments capture varying amounts of each call's loud broadband onset
    vs. quieter tail, producing a continuum rather than discrete types.
    A percentile range is the honest version of the same goal (capture the
    real spread of call content, not just its center) without pretending
    there's a cluster boundary that isn't there.

    Still flags individual calls whose low or high edge is a MAD-based
    outlier relative to the whole set -- informational only (see
    mad_outlier_mask), doesn't affect the proposed band.
    """
    lows = np.array([r["low_hz"] for r in rows])
    highs = np.array([r["high_hz"] for r in rows])

    low_outliers = mad_outlier_mask(lows, outlier_z)
    high_outliers = mad_outlier_mask(highs, outlier_z)
    outlier_mask = low_outliers | high_outliers

    percentiles = [10, 25, 50, 75, 90]
    return {
        "n_calls": len(rows),
        "n_outliers": int(outlier_mask.sum()),
        "outlier_rows": [r for r, flagged in zip(rows, outlier_mask) if flagged],
        "low_hz_percentiles": {p: float(np.percentile(lows, p)) for p in percentiles},
        "high_hz_percentiles": {p: float(np.percentile(highs, p)) for p in percentiles},
        "proposed_low_hz": float(np.percentile(lows, low_pct)),
        "proposed_high_hz": float(np.percentile(highs, high_pct)),
        "low_pct": low_pct,
        "high_pct": high_pct,
    }


def load_current_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH) as f:
        return json.load(f).get("species", {})


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples-dir", default=SAMPLES_DIR)
    parser.add_argument("--species", default=None,
                         help="Comma-separated species subset (default: every subdir of "
                              "--samples-dir except --exclude)")
    parser.add_argument("--exclude", default=",".join(DEFAULT_EXCLUDE),
                         help="Comma-separated species to skip")
    parser.add_argument("--threshold-factor", type=float, default=6.0,
                         help="Multiple of background-percentile energy a segment must exceed")
    parser.add_argument("--background-pct", type=float, default=20.0,
                         help="Percentile of the energy envelope used as the background estimate")
    parser.add_argument("--window-ms", type=float, default=20.0)
    parser.add_argument("--min-duration-ms", type=float, default=80.0)
    parser.add_argument("--merge-gap-ms", type=float, default=150.0)
    parser.add_argument("--pad-ms", type=float, default=30.0)
    parser.add_argument("--margin-hz", type=float, default=150.0,
                         help="Passed through to derive_band() -- matches the existing "
                              "config/species_tdoa_params.json convention")
    parser.add_argument("--min-low-hz", type=float, default=100.0,
                         help="Drop any scored segment whose derived low edge is at/below this "
                              "(proxy for 'no defined tonal content' -- see module docstring step 5)")
    parser.add_argument("--outlier-z", type=float, default=3.5,
                         help="Robust z-score (MAD-based) beyond which a call's band is flagged "
                              "(informational only, does not affect the proposed band)")
    parser.add_argument("--low-pct", type=float, default=10.0,
                         help="Percentile of low_hz used as the proposed band's low edge")
    parser.add_argument("--high-pct", type=float, default=90.0,
                         help="Percentile of high_hz used as the proposed band's high edge")
    parser.add_argument("--tmp-dir", default=None,
                         help="Scratch dir for mp3->wav conversions (default: a temp dir, cleaned up after)")
    parser.add_argument("--json-out", default=None, help="Optional path to dump the full structured report")
    args = parser.parse_args()

    exclude = {s.strip() for s in args.exclude.split(",") if s.strip()}
    if args.species:
        species_list = [s.strip() for s in args.species.split(",") if s.strip()]
    else:
        species_list = sorted(
            d for d in os.listdir(args.samples_dir)
            if os.path.isdir(os.path.join(args.samples_dir, d)) and d not in exclude
        )

    current_config = load_current_config()

    seg_kwargs = dict(
        window_ms=args.window_ms, threshold_factor=args.threshold_factor,
        background_pct=args.background_pct, min_duration_ms=args.min_duration_ms,
        merge_gap_ms=args.merge_gap_ms, pad_ms=args.pad_ms,
    )

    own_tmp = args.tmp_dir is None
    tmp_root = args.tmp_dir or tempfile.mkdtemp(prefix="derive_species_bands_")

    full_report = {}

    try:
        for species in species_list:
            species_dir = os.path.join(args.samples_dir, species)
            files = [os.path.join(species_dir, f) for f in sorted(os.listdir(species_dir))
                     if f.lower().endswith((".mp3", ".wav"))]
            if not files:
                print(f"\n=== {species} === (no sample files found, skipping)")
                continue

            print(f"\n=== {species} ({len(files)} sample file(s)) ===")
            kept, dedupe_log = dedupe_by_catalog(files)
            for line in dedupe_log:
                print(line)

            species_tmp = os.path.join(tmp_root, species.replace(" ", "_"))
            try:
                loaded = load_all(kept, species_tmp)
            except RuntimeError as e:
                print(f"  [FLAG] {e}")
                continue

            kept, content_dedupe_log = dedupe_by_content(loaded)
            for line in content_dedupe_log:
                print(line)

            all_rows = []
            for path in kept:
                rate, data, duration = loaded[path]
                rows, warnings = derive_bands_for_file(path, rate, data, args.margin_hz,
                                                         args.min_low_hz, seg_kwargs)
                print(f"  {os.path.basename(path)}: {len(rows)} call(s) scored")
                for w in warnings:
                    print(w)
                for r in rows:
                    print(f"    call {r['start_s']:.2f}-{r['end_s']:.2f}s "
                          f"({r['duration_s']*1000:.0f}ms): {r['low_hz']:.0f}-{r['high_hz']:.0f} Hz")
                all_rows.extend(rows)

            if not all_rows:
                print(f"  [FLAG] no calls scored for {species} at all -- cannot derive a band. "
                      f"Try a lower --threshold-factor or check the source files.")
                full_report[species] = {"rows": [], "aggregate": None}
                continue

            agg = aggregate_species(all_rows, args.outlier_z, args.low_pct, args.high_pct)
            full_report[species] = {"rows": all_rows, "aggregate": agg}

            print(f"  --- {species} summary ---")
            if agg["n_calls"] < 15:
                print(f"  [warn] only {agg['n_calls']} call(s) total -- percentile estimates are "
                      f"noisy at this sample size, weight the proposed band accordingly.")
            print(f"  {agg['n_calls']} call(s) total, {agg['n_outliers']} flagged as extreme "
                  f"outlier(s) (informational -- not excluded from the percentile range below):")
            for r in agg["outlier_rows"]:
                print(f"    [outlier] {r['file']} {r['start_s']:.2f}-{r['end_s']:.2f}s: "
                      f"{r['low_hz']:.0f}-{r['high_hz']:.0f} Hz")
            lp = agg["low_hz_percentiles"]
            hp = agg["high_hz_percentiles"]
            print(f"  low_hz  percentiles:  10%={lp[10]:.0f}  25%={lp[25]:.0f}  50%={lp[50]:.0f}  "
                  f"75%={lp[75]:.0f}  90%={lp[90]:.0f}")
            print(f"  high_hz percentiles:  10%={hp[10]:.0f}  25%={hp[25]:.0f}  50%={hp[50]:.0f}  "
                  f"75%={hp[75]:.0f}  90%={hp[90]:.0f}")
            print(f"  Proposed band ({agg['low_pct']:.0f}th pct low_hz to {agg['high_pct']:.0f}th pct high_hz): "
                  f"{agg['proposed_low_hz']:.0f}-{agg['proposed_high_hz']:.0f} Hz")

            current = current_config.get(species)
            if current:
                cur_low = current.get("freq_band_low_hz")
                cur_high = current.get("freq_band_high_hz")
                print(f"  Current config value: {cur_low}-{cur_high} Hz")
                if cur_low is not None and cur_high is not None:
                    print(f"  Delta: {agg['proposed_low_hz'] - cur_low:+.0f} Hz low, "
                          f"{agg['proposed_high_hz'] - cur_high:+.0f} Hz high")
            else:
                print(f"  Current config value: (none -- new species)")
    finally:
        if own_tmp:
            shutil.rmtree(tmp_root, ignore_errors=True)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(full_report, f, indent=2)
        print(f"\nWrote full report to {args.json_out}")

    print("\nReport only -- config/species_tdoa_params.json was not modified. "
          "Review the numbers above (especially any [FLAG]/[outlier]/[warn] lines) "
          "before transcribing them in.")


if __name__ == "__main__":
    main()
