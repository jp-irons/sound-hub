# Detection & TDOA Orchestration Pipeline

Status: steps 1-2 implemented; steps 3-6 designed, not yet built (see Milestones).
This doc didn't exist until 2026-06-29 — the pipeline sketch below was agreed in
conversation on 2026-06-28/29 but never got committed until now.

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
   *Solver implemented and tested, not wired* — `tdoa_solver.solve(nodes,
   timestamps_us)` (closed-form, `tdoa_solver.py:84`). `Node.x/y/z` map
   directly onto `node_positions.pos_e/pos_n/pos_alt`. Needs absolute arrival
   timestamps per node, derived by running the species' configured
   `onset_detection_method` against each node's WAV — only `global_peak`
   exists today (matches `clap_sync_check.py`'s `detect_onset`).
6. **Persist the result.**
   *No table exists yet.* Closest existing precedent is `trigger_events`
   (id, node_id, t_us, fired, `UNIQUE(node_id, t_us)`).

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
- **No runtime travel-time floor**: with nodes up to ~150m apart, max
  inter-node sound travel time is ~0.44s. `pull_window_s`/margins from
  `species_tdoa_params` should be floored at runtime to cover this,
  computed from `node_positions` geometry — not hardcoded, since geometry
  changes as nodes are added/moved. Not yet implemented anywhere.
- **No neighbour-selection logic**: "initially: all" was the agreed starting
  point, but there's no code that lists "all nodes with known positions and
  `approval_status == active`" as a single helper yet.

## Multi-bird-same-species disambiguation

Not yet designed. Open question: two individuals of the same species calling
near-simultaneously from different locations will both show up as the same
`species_key` in `audio_events`/`detections` — the solver has no way today
to know it's looking at two distinct sources rather than one ambiguous one.
Flagged, not scoped.

## Milestones (proposed 2026-06-29, none started)

Splitting steps 3-6 into four independently-shippable pieces rather than one
large change:

1. **`tdoa_attempts` table + species-lookup hook.** On a persisted top-species
   detection in `audio_push()`, call `get_effective_species_tdoa_params()`,
   compute the pull window (margins floored by max inter-node travel time),
   pick neighbour nodes, and write a `tdoa_attempts` row recording the plan —
   no actual pull yet. Self-contained, no node-side behavior change, fully
   inspectable via the DB before anything fires over the air.
2. **Wire the actual pull.** Issue `request_sample` to each planned neighbour,
   store their `requestId`s against the attempt row.
3. **Correlate arrivals.** When a pulled WAV lands back in `audio_push()`
   carrying a tracked `requestId`, run the species' onset-detection method on
   it, convert to an absolute timestamp, record it against the attempt.
4. **Solve + persist.** Once `min_corroborating_nodes` have reported, call
   `tdoa_solver.solve()`, write the result (or failure) back to the attempt
   row.

UI surfacing of results, and multi-bird disambiguation, are out of scope for
these four milestones — separate follow-on work.
