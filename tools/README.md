# tools/

Scripts for validating inter-node TDOA timing against real hardware. These
exist to answer a concrete question: given two nodes that each think they're
clock-synced, how much timing error actually shows up when they both record
the same real-world event?

Background and how this fits the wider design is in
`sound-capture-node/DESIGN.md` → **TDOA Localisation → Inter-Node Sync
Validation (Clap Test)**.

## The clap test

Method: make a single sharp acoustic event (a hand clap) audible to two
nodes, pull audio covering that moment from both, and cross-correlate to
measure the actual inter-node timing offset. Any offset beyond expected
clock-sync error (sub-millisecond, per `DESIGN.md`) indicates a problem in
GPS PPS discipline, ESP-NOW Kalman sync, or the pull path itself.

### `run_clap_test.py`

One-shot driver: pulls audio from two nodes via the hub and runs the
correlation. Two modes:

- **Legacy whole-window mode** — pulls the same `[tStart, tEnd]` window from
  both nodes and correlates the full buffers. Simple, but cross-correlating
  full multi-second buffers in a noisy outdoor environment can lock onto a
  room echo or an unrelated ambient transient instead of the clap, with
  nothing to flag that it happened.
- **Onset-refine mode** (default) — pulls a full coarse window from node A
  only, detects the clap's onset locally in A's audio, then pulls just a
  short window (`--onset-window-secs`, default 0.3s) from node B centered on
  that onset and correlates against the matching slice of A's
  already-downloaded buffer. Two downloads total, not three. Tunables:
  `--onset-threshold` (sanity-check multiple of background RMS, default 8.0)
  and `--onset-margin-ms` (refinement search margin, default 5.0).

### `clap_sync_check.py`

Shared correlation/reporting logic, plus the onset detector:

- `detect_onset()` finds the **global peak** of a short-time-energy envelope
  (not the first sample to cross threshold) and refines it to the
  steepest-rise raw sample within `margin_ms`. It picks the loudest
  transient in the buffer rather than the earliest one above threshold —
  important outdoors, where wind, insects, or handling noise can produce an
  earlier, quieter transient that isn't the clap.
- `_correlate_and_report()` cross-correlates two buffers, applies a parabolic
  interpolation for sub-sample peak precision, and flags ambiguous results
  (a peak that isn't clearly dominant — `_AMBIGUOUS_RATIO_THRESHOLD`) rather
  than reporting a misleadingly confident number.

## Status (2026-06-27)

The onset-refine flow and the `detect_onset()` global-peak fix have been
validated on synthetic test signals. Real-hardware re-validation against
soundcapture160/soundcapture170 is the next step — an earlier real-hardware
run reported unreliable correlations, which is what prompted the
global-peak fix.

## Relationship to node-side audio triggering

This onset-refine approach — cheaply finding "the interesting instant" in one
buffer and only pulling a narrow window from elsewhere — is also a working
prototype for the open question in `sound-capture-node/CLAUDE.md`: *"Can
ESP32-S3 nodes adequately determine criteria for audio segments that should
be sent to base station?"* The actual on-node trigger design (band-limited
energy + spectral flux) is tracked separately and isn't implemented yet.

## Species TDOA parameter portability

`species_tdoa_params` (per-species onset threshold, bandpass band, pull
window, etc. — see `docs/tdoa-correlation-design-notes.md`) only exists as
live rows in `sound_hub.db`, which is git-ignored and local to whichever
machine runs the hub. Since the acoustic-band portion of this tuning is a
property of the species, not the deployment, it's worth keeping portable
across instances rather than re-deriving it from scratch every time.
`../config/species_tdoa_params.json` is the version-controlled snapshot for
that — the DB stays the live working copy (edited via the Settings tab, or
`PUT /species-tdoa-params/{key}`), and the JSON file is a checkpoint of it.

### `export_species_params.py`

Dumps every species row (except the `__default__` sentinel — that one's
already owned by `FACTORY_DEFAULT_SPECIES_PARAMS` in `server/db.py`) from a
local `sound_hub.db` to `config/species_tdoa_params.json` by default. Run
this after tuning a species in the UI, then `git diff` the result before
committing — this script is the "prepare a commit" step, not a live sync.

### `import_species_params.py`

Reads `config/species_tdoa_params.json` and upserts its rows into a target
`sound_hub.db`. Existing species are skipped by default (so seeding a fresh
instance, or backfilling species an instance hasn't configured yet, can't
silently clobber local hand-tuning that's drifted from the tracked file) —
pass `--overwrite` to force a species back to the tracked file's values.
`--dry-run` previews changes; a y/N prompt guards the actual write.

### Caveat: not everything in the file is equally portable

`freq_band_low_hz`/`freq_band_high_hz` come from the reference recordings'
own spectral content and should transfer well across properties.
`onset_threshold_factor` is shaped by the *source recording's* background
noise floor (close-mic'd Xeno-canto clips read very differently from a
distant field mic) — treat the tracked file's threshold values as a
starting point for a new deployment, not a validated one, until checked
against that deployment's own pulled TDOA attempts.
