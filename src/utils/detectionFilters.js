// Shared date-range / moment filter logic and querystring building for
// the Detections tab and its species-summary list. Kept here so
// DetectionsTab.jsx and SpeciesSummaryList.jsx build identical params from
// the same filter state instead of duplicating the logic.

// Local-day boundaries (browser timezone) for the date-range presets.
export function startOfDay(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x }
export function endOfDay(d)   { const x = new Date(d); x.setHours(23, 59, 59, 999); return x }

// Calendar date-range presets — independent of "moment" below.
export const DATE_PRESETS = [
  { key: 'all',       label: 'All' },
  { key: 'today',     label: 'Today' },
  { key: 'yesterday', label: 'Yesterday' },
  { key: 'last7',     label: 'Last 7 days' },
  { key: 'custom',    label: 'Custom' },
]

// "Moment" options — a single-select group covering both sun-relative
// time-of-day buckets (kind: 'sun', resolved server-side per detection's
// calendar date) and rolling recency windows (kind: 'quick', resolved
// client-side from the current time). Grouped together because both answer
// "which moment" rather than "which day" — mixing e.g. "Last 10 min" with
// "Dawn" independently would mostly yield empty results and isn't a
// meaningful combination, so the UI now only lets one be active at a time.
export const MOMENT_OPTIONS = [
  { key: '',          label: 'All day',    kind: 'sun' },
  { key: 'last10min', label: 'Last 10 min', kind: 'quick' },
  { key: 'last1hour', label: 'Last hour',   kind: 'quick' },
  { key: 'dawn',       label: 'Dawn',       kind: 'sun' },
  { key: 'daytime',    label: 'Daytime',    kind: 'sun' },
  { key: 'dusk',       label: 'Dusk',       kind: 'sun' },
  { key: 'nighttime',  label: 'Nighttime',  kind: 'sun' },
]

export function momentKind(moment) {
  return MOMENT_OPTIONS.find(o => o.key === moment)?.kind ?? 'sun'
}

export function isQuickMoment(moment) {
  return momentKind(moment) === 'quick'
}

// Resolve the active moment + date preset to a {from, to} Date range. A
// quick moment (rolling window from "now") always wins over the calendar
// date preset — the two are mutually exclusive by construction in the UI,
// but resolveRange is defensive about it regardless. Returns null when
// there's no bound ('all' with no quick moment) or an incomplete custom
// range.
export function resolveRange(moment, datePreset, customFrom, customTo) {
  const now = new Date()
  if (moment === 'last10min') return { from: new Date(now.getTime() - 10 * 60 * 1000), to: now }
  if (moment === 'last1hour') return { from: new Date(now.getTime() - 60 * 60 * 1000), to: now }

  switch (datePreset) {
    case 'today':
      return { from: startOfDay(now), to: endOfDay(now) }
    case 'yesterday': {
      const y = new Date(now); y.setDate(y.getDate() - 1)
      return { from: startOfDay(y), to: endOfDay(y) }
    }
    case 'last7': {
      const start = new Date(now); start.setDate(start.getDate() - 6)
      return { from: startOfDay(start), to: endOfDay(now) }
    }
    case 'custom': {
      if (!customFrom && !customTo) return null
      return {
        from: customFrom ? startOfDay(new Date(customFrom)) : null,
        to: customTo ? endOfDay(new Date(customTo)) : null,
      }
    }
    default:
      return null // 'all'
  }
}

// Build the URLSearchParams shared by /detections and /detections/species-summary.
export function buildDetectionParams({ minConf, species, datePreset, customFrom, customTo, moment, limit }) {
  const params = new URLSearchParams({ limit, min_conf: minConf })
  if (species && species.trim()) params.set('species', species.trim())
  const range = resolveRange(moment, datePreset, customFrom, customTo)
  if (range?.from) params.set('from', range.from.toISOString())
  if (range?.to) params.set('to', range.to.toISOString())
  // Quick moments are expressed entirely as a from/to window above — the
  // server's time_of_day param only understands the sun-relative buckets.
  if (moment && momentKind(moment) === 'sun') params.set('time_of_day', moment)
  return params
}
