# Detections Tab Redesign — Implementation Plan

Status: **all 5 slices implemented**
Last updated: 2026-06-24

## Goal

Replace the current Detections tab (admin WAV upload + an unfiltered, unbounded
table of individual detections) with:

1. WAV upload relocated to its own admin-only tab.
2. A filter panel: date range (today / yesterday / last 7 days / custom) and
   time-of-day (dawn / dusk / daytime / nighttime / all day), the latter
   computed from real sunrise/sunset for the node's location rather than fixed
   clock hours.
3. A collapsed-by-default, per-species list (count, last-seen) instead of a
   flat detection table. Species rows expand to show individual detections.
4. Per-species pin (checkbox) to float to top, plus most/least-frequent sort.

## Current state (for reference)

- Tabs: Map / Detections / Users only (`src/App.jsx:318-323`). No Tools/Admin
  tab exists yet.
- Upload UI lives inside `DetectionsTab.jsx:160-218`, posts to
  `POST /detections/analyze` (routes.py:937-938).
- `GET /api/detections` (routes.py:930-934) only honors `limit` — the
  frontend already sends `species`/`min_conf` (DetectionsTab.jsx:59-61) but
  the backend silently ignores them. This is a pre-existing bug, not new
  scope, but slice 2 fixes it as a side effect.
- No species-aggregation endpoint exists anywhere in `routes.py`/`db.py`.
- `DetectionRecord` (models.py:183-195) has `node_id`, `analyzed_at`,
  `common_name`, `scientific_name`, `confidence`, `start_sec`, `end_sec` —
  enough to support everything below without a schema change.

## Slicing approach

Each slice is independently shippable and demoable. Within a slice, build the
UI first (against mock/static data where the real endpoint doesn't exist
yet), confirm the interaction feels right, then wire it to a real backend
endpoint. This keeps feedback fast and avoids backend work for a UX shape
that might change once it's on screen.

---

### Slice 1 — Move WAV upload to its own tab — **done**

**Why first:** zero dependencies, immediate declutter, low risk.

- Add a 4th tab ("Tools", admin-only) in `App.jsx` next to Users.
- Move the upload block (`DetectionsTab.jsx:160-218`) into a new
  `ToolsTab.jsx`, unchanged otherwise (same `/detections/analyze` call).
- Detections tab loses the upload zone entirely.

No backend changes. Acceptance: upload still works from its new home;
Detections tab no longer shows it.

---

### Slice 2 — Date-range filter (UI first, then real backend) — **done**

- **UI:** add a filter bar above the (still-flat, for now) table: preset
  buttons (today / yesterday / last 7 days) + a custom range picker. Wire it
  to client-side filtering of the already-fetched rows first, to validate the
  interaction.
- **Backend:** extend `GET /api/detections` to accept `from`/`to` (ISO
  timestamps), and actually honor `species`/`min_conf` while we're in there
  (fixes the existing dropped-filter bug). Update `db.list_detections` to
  build a proper `WHERE` clause.
- **Wire-up:** switch the UI filter from client-side to query params.

Acceptance: selecting "last 7 days" returns only matching rows from the
server, not just a client-side slice of the capped 200/2000-row fetch.

---

### Slice 3 — Sun-relative time-of-day filter — **done**

This is the piece you flagged as separable — treat it as its own
self-contained unit of work since it has no UI dependency.

- **Backend:** small helper (`server/suntimes.py`) using the `astral`
  library, taking a date + lat/lon and returning dawn/dusk/day/night window
  boundaries for that date, with a buffer around actual sunrise/sunset for
  the dawn/dusk windows (30 min before / 90 min after sunrise; 90 min before
  / 30 min after sunset — asymmetric to match the dawn chorus building for
  longer than the shorter, front-loaded evening chorus). Lat/lon comes from
  the existing property-level **`array_origin`** table (`db.py:265`,
  `get_array_origin()`) — the same reference datum already used for the
  cartesian node-position math — not a per-node GPS average. One sun-time
  calculation per day covers the whole property regardless of node count.
- Expose it as a query param on `/api/detections`
  (`time_of_day=dawn|dusk|daytime|nighttime|all`), resolved server-side per
  detection's date (a multi-day range spans varying sun times, so this must
  be computed per-day, not once for the range).
- **UI:** add the dawn/dusk/daytime/nighttime/all-day selector to the filter
  bar from slice 2, wired directly to the new param (no client-only stage
  needed here since the UI is trivial — a button group).

**Built differently from the original plan in one important way:** the local
timezone used to resolve each detection's calendar date (and therefore which
day's sunrise/sunset it's classified against) is derived dynamically from the
`array_origin` lat/lon via `timezonefinder`, not hardcoded to
`Australia/Brisbane`. This was a deliberate correction mid-slice — see
Decisions log — because the system is intended to also run a second hub from
a van at varying campsite locations, re-surveying `array_origin` at each new
site. Hardcoding the timezone would have silently broken (or misclassified)
results the moment the van hub moved to a different timezone. `classify_many`
resolves the timezone once per request rather than once per row.

Acceptance: filtering "dawn" on a winter date and a summer date returns
detections from genuinely different clock-time windows. Verified for both
Brisbane and Denver, CO (opposite hemisphere/season, DST-observing) to confirm
the dynamic-timezone path doesn't regress on the original target location.

---

### Slice 4 — Per-species summary list (replaces the flat table) — **done**

- **UI:** `SpeciesSummaryList.jsx` — collapsed rows (name, count, last-seen),
  expand fetches that species' individual detections (`species=` scoped,
  then exact-matched client-side against `commonName` so two species
  sharing a name substring never bleed into each other's expanded rows) and
  renders them in a small inline table. Shared filter-building and row
  formatting were pulled out of `DetectionsTab.jsx` into
  `src/utils/detectionFilters.js` and `src/components/DetectionFormat.jsx` so
  this component and the main tab build identical params/rows from one
  source.
- **Backend:** `GET /api/detections/species-summary` — same date-range +
  min_conf + species filters as `/api/detections`, returning per-species
  `{common_name, scientific_name, count, last_seen, avg_confidence}`.

**Built differently from the original plan in one way:** `time_of_day` could
not be added to the same `GROUP BY` query, because classification is
per-row (each detection's bucket depends on its own calendar date — see
slice 3) and can't be expressed as a SQL range. When `time_of_day` is set,
the endpoint instead fetches up to 2000 raw rows (same ceiling
`/api/detections` uses), classifies them, and aggregates in Python. **Known
limitation, not yet fixed:** for a site with more than 2000 matching
detections in the selected date range, per-species counts under a
`time_of_day` filter could undercount. A more scalable version would compute
each calendar day's sun windows and push them into the SQL `WHERE` as a
union of ranges, avoiding the row cap — worth doing if/when detection volume
makes this bite in practice.

Acceptance: species list loads fast even with thousands of underlying
detections when no `time_of_day` filter is active, because aggregation
happens in SQL, not in the browser. (See the row-cap caveat above for the
`time_of_day` + species-summary combination.)

---

### Slice 5 — Pin and sort — **done**

Pure frontend, sits on top of slice 4 (`SpeciesSummaryList.jsx`).

- Checkbox per species row to pin; pinned species render in their own
  section above the rest, each section sorted by the active sort mode.
- Sort control: most frequent / least frequent / most recently seen /
  alphabetical (A–Z).
- Pinned set persisted in `localStorage` (`detections.pinnedSpecies`) and
  sort mode persisted separately (`detections.sortMode`) — both survive a
  reload and intentionally survive filter changes too, since pinning is
  meant to be a standing preference, not scoped to one filter combination.

Acceptance: pinning a species keeps it pinned across a page refresh.

---

### Future / out of scope for this plan

- Per-species node breakdown (which node(s) detected it) — `node_id` is
  already stored, just unused in the UI. Easy add-on to slice 4 once the
  summary endpoint exists.
- Audio playback/snippet preview from expanded detection rows.
- Saved filter presets (beyond the built-in date/time-of-day ones).

## Decisions log

- 2026-06-24: time-of-day windows will be sun-relative (computed per date),
  not fixed clock hours — confirmed by Jon.
- 2026-06-24: sun calculation uses the property-level `array_origin` lat/lon
  (`db.py:265`), not per-node GPS — confirmed by Jon. This is the same
  reference datum already used for cartesian node positioning.
- 2026-06-24: dawn/dusk buffers set to 30 min before / 90 min after sunrise
  (dawn) and 90 min before / 30 min after sunset (dusk) — confirmed by Jon.
  Asymmetric because the dawn chorus builds for an hour-plus after sunrise as
  birds disperse to forage, while the evening chorus is shorter and
  front-loaded into the run-up to sunset.
- 2026-06-24: the local timezone for sun-time classification must be derived
  dynamically from the `array_origin` lat/lon (via `timezonefinder`), not
  hardcoded to `Australia/Brisbane` as first implemented. Corrected after Jon
  flagged that the system will also run a second hub from a van at varying
  campsite locations, re-surveying `array_origin` at each new site — the
  whole point is that time-of-day logic follows wherever `array_origin`
  currently points, with no Brisbane-specific code. See `suntimes.py`'s
  `local_tz_for()`.
- 2026-06-24: the species-summary endpoint's `time_of_day` path is capped at
  2000 raw rows before aggregating in Python (see slice 4) — accepted as a
  v1 limitation rather than building the more scalable per-day SQL range
  union now. Not yet explicitly confirmed by Jon as a long-term acceptable
  tradeoff — revisit if detection volume makes the cap bite in practice.
- 2026-06-24: pinned species and sort mode persist across filter changes,
  not just reloads — pinning is a standing preference, not scoped to one
  filter view.

## Open questions

- Should `array_origin` carry a hub identifier to support the Brisbane
  property and the van running simultaneously against the same database, or
  is each hub expected to run its own separate database/instance? Raised
  during slice 3, not yet answered.
