# Detection & TDOA Orchestration Pipeline

Status: all four milestones (steps 3-6) implemented as of 2026-07-11 — see
Milestones below and "Known gaps after milestones 3+4" for what's
deliberately left unsolved. This doc didn't exist until 2026-06-29 — the
pipeline sketch below was agreed in conversation on 2026-06-28/29 but never
got committed until now. Milestone 7 (2026-07-28) fixes three real,
connected bugs found in production TDOA solving — see that section for
what's fixed vs. still deliberately deferred.

## Pipeline

1. **Node trigger fires** → node pushes a short window (pre-roll + post-roll,
   currently 1s/2s) to the hub, untargeted/unfiltered.
   *Implemented* — `EspNowControl.cpp`'s `audioPushTask()` on the node side.
2. **Hub runs BirdNET on every triggered push.**
   *Implemented* — `audio_push()` in `server/routes.py` calls
   `birdnet_worker.analyze_wav_full()`, persists detections above
   `DEFAULT_MIN_CONF` (0.5) to the `detections` table, and writes one
   `audio_events` row per push with `top_species`, `top_confidence`,
   `detection_count`, and the **absolute** capture window
   (`t_start_us`/`t_end_us`, fixed 2026-06-29 to reflect what was actually
   captured, not what was requested — see `AudioStore::Snapshot`'s
   `actualStartUs`/`actualEndUs`).
3. **On a species detection, look up that species' TDOA params.**
   *Implemented (table only)* — `species_tdoa_params` table + CRUD
   (`db.py`/`models.py`/`routes.py`), admin UI in `SettingsTab.jsx`.
   `db.get_effective_species_tdoa_params(species_key)` returns
   `(params, used_default: bool)`, falling back to the `__default__` sentinel
   row when a species has no row or its row is disabled.
   *Not implemented*: nothing in `audio_push()` actually calls this yet. No
   hook point exists — detection persistence (`routes.py` ~line 808-819) is
   fire-and-forget today.
4. **Pull the same window from neighbour nodes.**
   *Mechanism exists, not wired to step 3* — `POST /api/nodes/{id}/sample`
   (`request_sample`, `routes.py:835-905`) is async: takes an absolute
   `t_start_us`/`t_end_us` window, relays the request via broker → ESP-NOW,
   returns `{requestId, status: "relayed"}` immediately (202). The WAV
   arrives later via the *same* `audio_push()` endpoint, tagged with that
   `requestId`. No code currently issues these calls automatically from a
   detection.
5. **Solve TDOA.**
   *Implemented (milestone 4, 2026-07-11)* — `tdoa_solver.solve(nodes,
   timestamps_us)` (closed-form, `tdoa_solver.py:84`), called from
   `routes.py`'s `_maybe_solve_tdoa_attempt_inner` once enough nodes have
   arrival timestamps. `Node.x/y/z` map directly onto
   `node_positions.pos_e/pos_n/pos_alt`. Absolute arrival timestamps per node
   are derived by `server/onset_detection.py` running the species' configured
   `onset_detection_method` against each node's WAV — only `global_peak`
   exists today (ported from `clap_sync_check.py`'s `detect_onset`; see
   "Known gaps" below for what that does *not* yet include, e.g. bandpass
   filtering).
6. **Persist the result.**
   *Implemented (milestone 4, 2026-07-11)* — solve columns added directly to
   `tdoa_attempts` (`solved_e/n/alt`, `solve_residual_m`, `solve_method`,
   `solve_ambiguous_json`, `solved_at`) rather than a separate table, written
   by `db.persist_tdoa_solution()`.

## Gaps to close

- **No hook point**: `audio_push()` needs to call into orchestration logic
  after persisting a detection. Likely a new function called inline, not a
  real event bus (matches this codebase's synchronous style elsewhere).
- **No results table**: needs to track, per detection: which species params
  were used (and whether they were the `__default__` fallback —
  `used_default` should be visible, not just logged, per the original ask),
  which neighbour nodes were asked, which responded, the per-node arrival
  timestamps, and the final `SolveResult` (or failure reason).
- **No requestId → attempt linkage**: `request_sample`'s `_audio_requests`
  dict is in-memory and per-request; nothing currently associates multiple
  outstanding `requestId`s with a single originating detection.
- **Runtime travel-time floor**: *Done (2026-07-17).* Margins from
  `species_tdoa_params` are floored per-attempt from live `node_positions`
  geometry, not hardcoded — see `_max_pairwise_distance_m` /
  `travel_time_floor_s` in `_plan_tdoa_attempt_inner`. Additive with the
  species' configured margin (not `max()`'d against it) as of the same
  date — the two represent different things (onset-detection slop vs.
  propagation-delay worst case) and should both apply, not whichever is
  larger. `pull_window_s` (a separate fixed-minimum-total-duration knob)
  was removed the same day — it silently overrode this computation
  whenever the fixed 3.0s floor it enforced came out larger, and on this
  ~3Ha property that was most of the time.
- **No neighbour-selection logic**: "initially: all" was the agreed starting
  point, but there's no code that lists "all nodes with known positions and
  `approval_status == active`" as a single helper yet.

## Multi-bird-same-species disambiguation

Not yet designed. Open question: two individuals of the same species calling
near-simultaneously from different locations will both show up as the same
`species_key` in `audio_events`/`detections` — the solver has no way today
to know it's looking at two distinct sources rather than one ambiguous one.
Flagged, not scoped.

## Milestones (proposed 2026-06-29, milestones 1-4 implemented 2026-07-11, milestone 5 added 2026-07-17)

Splitting steps 3-6 into independently-shippable pieces rather than one
large change:

1. **`tdoa_attempts` table + species-lookup hook.** *Done.* On a persisted
   top-species detection in `audio_push()`, calls
   `get_effective_species_tdoa_params()`, computes the pull window (species
   margin plus max inter-node travel time, additive — see "Runtime
   travel-time floor" above; `pull_window_s` briefly existed as a separate
   fixed-minimum-duration knob from 2026-07-11 but was removed 2026-07-17),
   picks neighbour nodes, and writes a `tdoa_attempts` row recording the
   plan.
2. **Wire the actual pull.** *Done.* Issues `request_sample` to each planned
   neighbour, stores their `requestId`s against the attempt row
   (`tdoa_attempt_nodes`).
3. **Correlate arrivals.** *Done.* `server/onset_detection.py` runs the
   species' onset-detection method (`global_peak` only, ported from
   `tools/clap_sync_check.py`'s `detect_onset`) against a node's WAV and
   converts the detected sample to an absolute node-clock timestamp.
   Runs from two places: inline during planning for nodes whose WAV already
   exists (the origin's own trigger, known reporters from detection-
   coalescing, and reused-existing neighbours — `_plan_tdoa_attempt_inner`),
   and from `audio_push()` when a freshly-pulled WAV lands, matched back to
   its attempt via `requestId` (`db.find_tdoa_attempt_node_by_request_id`).
4. **Solve + persist.** *Done.* Once enough `tdoa_attempt_nodes` rows reach
   `status='arrived'` to satisfy `min_corroborating_nodes`
   (`_maybe_solve_tdoa_attempt`), calls `tdoa_solver.solve()` and writes the
   result — solved position + residual + method, or a mirror root, or a
   `failed` status with reason — back onto the `tdoa_attempts` row
   (`db.persist_tdoa_solution`).
5. **Leading-edge cross-correlation refinement.** *Implemented 2026-07-17,
   deployed 2026-07-19.* Milestone 3's `arrival_us` came from each node's
   `detect_onset_us()` running independently — an amplitude/energy-threshold
   detector exposed to loudness/SNR differences between nodes at different
   distances from the same call (a real, measured effect —
   `docs/tdoa-correlation-design-notes.md` §§1-3). `server/correlation.py`
   (ported from `tools/clap_sync_check.py`'s `trim_to_leading_edge()`/
   `_score_correlation()`, implementing both `plain` and `gcc_phat`) now
   refines each non-origin node's estimate by cross-correlating a short
   leading-edge window of its WAV against the origin's own WAV, both
   anchored on `origin_arrival_us` via each buffer's own `t_start_us` (the
   two WAVs do not share a sample-index timebase — origin's is a raw
   trigger capture, not pulled against the shared window). `_correlate_attempt_node`
   (`routes.py`) uses the correlated value only when it clears a confidence
   gate (`peak_corr_coef`/`quality_ratio`, thresholds carried over unchanged
   from `clap_sync_check.py`'s clap-test-validated values); otherwise it
   keeps the independent-detector estimate, so this can only ever make a
   corroborating node's arrival more accurate, never worse than pre-
   milestone-5 behaviour. `species_tdoa_params.correlation_method` was
   previously stored/snapshotted but never read by anything (dead config,
   defaulted to `gcc_phat`) — it's now actually consumed, and the default
   changed to `plain` per §7 below. See `project_bird_tdoa_correlation_gap`
   memory for the full investigation and a same-day follow-up (pull-margin
   formula changed to additive, `pull_window_s` removed, Settings UI
   dropdown fixed).
6. **Node-reported noise floor.** *Implemented 2026-07-19, not yet
   deployed/field-validated.* Milestone 3's onset detection estimates each
   clip's background as `np.median(rms)` over the clip itself — unreliable
   for TDOA corroboration pulls specifically, since their pre/post margins
   are narrow by design (geometry-driven, see §"Runtime travel-time floor"
   above) and can leave too little genuine quiet background in the clip to
   median over. `sound-capture-node`'s `NoiseFloorTracker` (new class,
   `main/audio/`) tracks a broadband time-domain RMS ambient floor
   continuously on the node (fast/floor EMA with freeze-above-ratio, same
   shape as `AudioTrigger`'s gates but a much longer ~180s tau — deliberately
   decoupled from `AudioTrigger`'s own 30s, since this is meant to track a
   slower-moving property). `AudioPullHandler` reports it on every pull via
   a new `X-Noise-Floor-Rms` response header (plus `X-Noise-Floor-Energy-
   High`/`-Low`, `AudioTrigger`'s existing FFT-band floors, exposed for
   future use but not yet consumed). `_fetch_audio_direct` reads the header
   and threads it through `_save_direct_pull_audio` → `_correlate_attempt_node`
   → `onset_detection.detect_onset_us`'s new `node_noise_floor_rms` param,
   which converts int16 scale to soundfile's normalized `[-1.0, 1.0)` scale
   and substitutes it for the clip-derived median when present.
   Deliberately does **not** yet change any per-species margins — every call
   logs the clip-derived and node-reported values side by side
   (`onset_detection.py`'s `detect_onset`) so real pull data accumulates for
   comparison before the tau or the override itself is trusted, same
   validate-before-trust discipline as `AudioTrigger`'s own gate tuning (see
   `project_bird_audio_event_trigger_design` memory). Only origin/known-
   reporter/reused-existing WAVs still fall back to the clip-derived median
   (no pull-response header exists for those paths) — unchanged behaviour.
   See `project_bird_noise_floor_reporting` memory.

7. **TDOA solve-quality fixes (2026-07-28).** Three real, connected bugs
   found in one session, triggered by Jon's skepticism that no real solve
   was ever landing anywhere near the array:
   - **Timestamp-precision catastrophic cancellation, `tdoa_solver.py`.**
     `solve()` scaled raw absolute Unix-epoch microsecond timestamps
     directly (`d = c*t*1e-6 ≈ 6e11m`), so the linear system's `d0²-di²`
     terms cancelled out the real few-metre signal before the solve ever
     ran. **Fixed** — anchor to `min(timestamps_us)` before scaling.
     Verified against 5 real attempts: 4/5 resolved to 2-18m from the array
     (previously millions of metres). This very likely explains most of
     what earlier investigations (§§6-7 above,
     `docs/tdoa-correlation-design-notes.md`) attributed to correlation or
     aperture causes — see correction blocks added to the
     `project_bird_tdoa_implausible_solves`/`project_bird_tdoa_baseline_
     aperture_limit` memories, and `project_soundhub_timestamp_precision_
     bug` for the full writeup. Jon explicitly authorized this one edit
     directly, departing from the repo's normal propose-first rule for
     this instance only.
   - **Correlation ambiguity-ratio search unbounded, `correlation.py`.**
     `_peak_quality()` searched the *entire* `correlate(mode='full')`
     output for a competing peak, including lags the sound could not
     physically have produced. Real diagnostic: 12/19 node pairs across 5
     attempts had their ambiguity-suppressing "competitor" outside the
     physical max-transit bound. **Fixed and deployed** — search now
     bounded to `+/- transit_s` when geometry is known
     (`_peak_quality`/`_score_correlation`/`correlate_leading_edge` all
     gained/threaded a `transit_s` param). Trusted correlations went
     4/19 → 14/19 on the same test set. Deferred: a second, unvalidated
     change (capping the correlation TEMPLATE width, independent of the
     pull-margin knee distance in `window_margin_pre/post_ms`) targets the
     remaining ~5 pairs whose ambiguity is genuine (call repetition inside
     the physical bound, not a search-window artifact) — needs a real
     multi-species data sweep, not a guessed constant, before a default is
     chosen. See `project_soundhub_correlation_ambiguity_bound` memory.
   - **Independent onset detection gated corroborating-node correlation,
     `routes.py` `_correlate_attempt_node`.** A corroborating node's own
     `detect_onset_us()` had to succeed before `correlate_leading_edge` was
     even attempted — backwards, since the origin's onset (all correlation
     needs) is established at cluster-fire time in `_fire_cluster_after_
     delay`, before any corroborating pull is even requested (see
     milestone 1 above). **Fixed** — onset detection is now non-blocking
     (still run and logged, still feeds `onset_threshold_factor` p90
     tuning), correlation always attempted when the origin is known. A
     node only becomes `onset_failed` now if neither method produces
     anything usable. Jon reviewed the diff and is deploying it himself.
     Deferred, related: `_maybe_solve_tdoa_attempt_inner` still feeds every
     `arrived` row into `tdoa_solve()` regardless of `correlation_status`
     (`uncorrelated_node_count` is informational only, not a filter) —
     restricting the solver to `trusted`-only rows is a natural next step
     but explicitly held back ("see what happens first") until the above
     is observed live. See `project_soundhub_onset_gating_removed` memory.

UI surfacing of results is **done** (2026-07-11, separate follow-on session)
— `GET /api/tdoa/attempts` (viewer-level) returns recent attempts with their
per-node correlation rows embedded; the frontend's Analytics tab gained a
third "Localisation" sub-tab (alongside "Push events"/"Trigger diagnostics")
showing status, corroborating-node count, solved position + residual (or
failure reason), with a per-attempt disclosure row for per-node arrival
detail and the mirror-root flag when a 4-node solve is ambiguous.

Multi-bird disambiguation is still out of scope — separate follow-on work.

## Known gaps after milestones 3+4 (2026-07-11)

Deliberately not solved by the milestone-3/4 implementation — flagging so
they're visible rather than discovered by surprise later:

- **`min_corroborating_nodes` counts the origin as one of the total, not
  "origin plus this many corroborators".** With the field's own default of
  4, an attempt solves as soon as 4 total nodes (including the origin) have
  arrival timestamps — that's the 4-node **quadratic** solve, which is
  mirror-root ambiguous (`tdoa_solver.py`). The unambiguous 5+-node
  least-squares case needs `min_corroborating_nodes=5`, not 4.
- **Bandpass filtering + onset threshold are tunable (updated 2026-07-11,
  bands populated 2026-07-13 — this bullet was stale, corrected 2026-07-17).**
  `docs/tdoa-correlation-design-notes.md` found bandpass filtering recovers a
  71-78% onset-detection miss rate down to ~10% for real bird calls at
  realistic SNR — that finding is wired into `onset_detection.py`
  (`bandpass_filter()`, ported from `tools/synthetic_snr_feasibility.py`),
  and `onset_threshold_factor` (previously a hardcoded 8.0, tuned only for
  hand-clap validation tests) is a per-species DB column alongside
  `freq_band_low_hz`/`freq_band_high_hz`, editable from the Settings tab.
  Both are snapshotted onto `tdoa_attempts` at plan time like
  `onset_detection_method`. All 4 currently-configured species (Gray
  Butcherbird, Noisy Miner, Pied Currawong, Torresian Crow) have derived
  bands and tuned thresholds as of 2026-07-13 — see
  `config/species_tdoa_params.json` / `tools/README.md`. Bandpass is now
  also applied ahead of milestone 5's leading-edge correlation, not just
  onset detection (`tdoa-correlation-design-notes.md` §7-8 found it helps
  correlation directly). Still open: `onset_threshold_factor`/band values
  were derived from reference recordings, not validated against real pulled
  attempts on this property yet (each species' `notes` field says so).
- **`species_tdoa_params.correlation_method` was dead config until
  2026-07-17.** Stored, snapshotted onto attempts, exposed via the API/UI,
  but nothing read it to actually run a correlation — the field described a
  choice that had no effect. Fixed alongside milestone 5 above (now
  consumed by `correlation.py`); the UI dropdown's `onset_envelope` option
  — anticipated as a future third method in `sound-capture-node/DESIGN.md`'s
  "Per-species correlation mechanism" section (better for periodic/
  narrowband calls at risk of cycle-slip/phase ambiguity, e.g. Pheasant
  Coucal) but never actually implemented here — was removed from the
  dropdown the same day since selecting it silently degraded a species to
  always-fallback with no visible error. Re-add once (if) it's built.
- **No `hint_point` wired into the automatic solve.** Nothing in production
  config defines one (only the manual `POST /api/tdoa/solve` route accepts
  one ad-hoc per-request). A 4-node solve's mirror root is stored
  (`tdoa_attempts.solve_ambiguous_json`) for manual review, not
  auto-resolved.
- **No watchdog for an attempt stuck at `'pulling'`.** If enough neighbours'
  pulls land but onset detection fails on enough of them (too quiet, no
  transient), the attempt never reaches `min_corroborating_nodes` and never
  advances to `'failed'` either — it just sits at `'pulling'` indefinitely.
- **Pre-migration `audio_events` rows have no `filename`.** Correlation
  against a `reused_existing` audio event written before the `filename`
  column existed fails onset detection (logged as `onset_failed`, not
  fatal) — only rows written after this change carry it.
