// Shared date-range / time-of-day filter logic and querystring building for
// the Detections tab and its species-summary list. Kept here so
// DetectionsTab.jsx and SpeciesSummaryList.jsx build identical params from
// the same filter state instead of duplicating the logic.

// Local-day boundaries (browser timezone) for the date-range presets.
export function startOfDay(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x }
export function endOfDay(d)   { const x = new Date(d); x.setHours(23, 59, 59, 999); return x }

export const DATE_PRESETS = [
  { key: 'all',       label: 'All' },
  { key: 'last10min', label: 'Last 10 min' },
  { key: 'last1hour', label: 'Last hour' },
  { key: 'today',     label: 'Today' },
  { key: 'yesterday', label: 'Yesterday' },
  { key: 'last7',     label: 'Last 7 days' },
  { key: 'custom',    label: 'Custom' },
]

export const TIME_OF_DAY_OPTIONS = [
  { key: '',          label: 'All day' },
  { key: 'dawn',       label: 'Dawn' },
  { key: 'daytime',    label: 'Daytime' },
  { key: 'dusk',       label: 'Dusk' },
  { key: 'nighttime',  label: 'Nighttime' },
]

// Resolve a preset key (or explicit custom from/to date strings) to a
// {from, to} Date range. Returns null for 'all' (no bound) or an incomplete
// custom range.
export function resolveRange(preset, customFrom, customTo) {
  const now = new Date()
  switch (preset) {
    case 'last10min':
      return { from: new Date(now.getTime() - 10 * 60 * 1000), to: now }
    case 'last1hour':
      return { from: new Date(now.getTime() - 60 * 60 * 1000), to: now }
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
export function buildDetectionParams({ minConf, species, datePreset, customFrom, customTo, timeOfDay, limit }) {
  const params = new URLSearchParams({ limit, min_conf: minConf })
  if (species && species.trim()) params.set('species', species.trim())
  const range = resolveRange(datePreset, customFrom, customTo)
  if (range?.from) params.set('from', range.from.toISOString())
  if (range?.to) params.set('to', range.to.toISOString())
  if (timeOfDay) params.set('time_of_day', timeOfDay)
  return params
}
