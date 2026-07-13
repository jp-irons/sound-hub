// Audio pipeline analytics — visibility into every push to POST /api/audio/push,
// regardless of BirdNET outcome. Pairs with DetectionsTab (which only ever shows
// the hits): this tab is for answering "is the node trigger firing enough?" and
// "is BirdNET seeing candidates that just fall below threshold?"
import { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '../auth.js'
import { formatTime, formatDateTime } from './DetectionFormat.jsx'
import { useIsMobile } from '../hooks/useBreakpoint.js'
import { MOMENT_OPTIONS, isQuickMoment, startOfDay, endOfDay } from '../utils/detectionFilters.js'

const EVENTS_LIMIT = 200

// Trigger-diagnostics summary fetch no longer needs the raw `events` array
// (the per-block detail table it fed was replaced by the ratio histogram
// below — a sustained call flooded that table with near-identical near-miss
// rows and buried the one row that mattered, see project discussion). Ask
// for the minimum the backend will accept rather than paying for a payload
// nothing renders.
const TRIGGER_SUMMARY_EVENTS_LIMIT = 1

// Raw trigger_events (and so the ratio histogram) only survives
// TRIGGER_EVENTS_RETENTION_HOURS server-side before being pruned down to
// per-minute rollups that can't reconstruct a distribution — the histogram
// fetch clamps its lower bound to this regardless of what range is selected.
const HISTOGRAM_MAX_LOOKBACK_MS = 6 * 60 * 60 * 1000

// Forked from detectionFilters.js's DATE_PRESETS/resolveRange, deliberately
// diverging from the Detections tab rather than sharing the component (see
// project discussion) — this tab dropped "Last 7 days" (not useful against
// either the histogram's 6h cap or what's actually been used to investigate
// trigger behaviour) and Custom takes full date+time instead of whole-day
// granularity, since the real use case here is a short, specific window
// ("did anything fire between 2:00 and 2:30am"), not picking whole calendar
// days.
const TRIGGER_DATE_PRESETS = [
  { key: 'all',       label: 'All' },
  { key: 'today',     label: 'Today' },
  { key: 'yesterday', label: 'Yesterday' },
  { key: 'custom',    label: 'Custom' },
]

function resolveTriggerRange(moment, datePreset, customFrom, customTo) {
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
    case 'custom': {
      if (!customFrom && !customTo) return null
      // customFrom/customTo are datetime-local strings ("YYYY-MM-DDTHH:mm")
      // — parsed directly as local time by the Date constructor, no
      // start/end-of-day snapping needed since the user picked exact times.
      return {
        from: customFrom ? new Date(customFrom) : null,
        to: customTo ? new Date(customTo) : null,
      }
    }
    default:
      return null // 'all'
  }
}

function relativeTime(isoString) {
  if (!isoString) return '—'
  const diff = Date.now() - new Date(isoString).getTime()
  if (diff < 60000) return `${Math.floor(diff / 1000)}s ago`
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`
  return `${Math.floor(diff / 86400000)}d ago`
}

// trigger_events timestamps are node-clock microseconds (t_us), not ISO
// strings — convert once at the call site so the shared formatTime /
// formatDateTime / relativeTime helpers (which all expect ISO) still work.
function tUsToIso(tUs) {
  if (tUs == null) return null
  return new Date(tUs / 1000).toISOString()
}

// Renders one ratio histogram (energy or flux) as inline SVG — no charting
// library needed for a couple of bar series. `threshold` is the gate's fire
// ratio (kTriggerRatioEnergy=6.0 / kTriggerRatioFlux=2.0 in AudioTrigger.hpp)
// drawn as a reference line. Bars are stacked: muted = total (near-miss +
// fired) count in that bucket, green = the fired portion of it.
// Fired counts are routinely 1000:1 (or worse) against near-miss counts in
// the same bucket (see project history — hundreds of fires/day against
// 373k+ near-miss blocks) — a strictly proportional bar height renders a
// real fire as a fraction of a pixel. MIN_FIRED_PX floors any nonzero fired
// segment to a visible height; it never overstates the count itself, since
// the exact count is always also printed as a label above the bar.
//
// MIN_BAR_PX floors the *total* bar the same way, for a reason that isn't
// obvious until you hit it: a bucket can be entirely fires with a tiny total
// count (e.g. energy ratio 0-1 — below the 1.5 interesting-ratio floor, so
// no near-miss row can ever land there; only a low-band fire, whose
// high-band energy_ratio can be near zero, appears here at all — see the
// caption below the charts). Without MIN_BAR_PX, that bucket's *total* bar
// height is itself sub-pixel (tiny count against the global max), and the
// fired segment's `Math.min(barHeight, ...)` clamp was capping the fired
// floor down to that sub-pixel total — defeating MIN_FIRED_PX exactly where
// it mattered most (a 100%-fired bucket). A larger, mixed near-miss+fire
// bucket doesn't hit this, since its own total is already comfortably above
// both floors.
const MIN_BAR_PX = 2
const MIN_FIRED_PX = 1

// Compact count formatting for bar labels — "1k5" = 1,500, "1M5" = 1,500,000
// (one decimal digit folded into the unit letter, per the user's requested
// notation, rather than "1.5k"). Values under 1000 print as plain integers.
function formatCompactCount(n) {
  const units = [
    { value: 1_000_000_000, suffix: 'B' },
    { value: 1_000_000, suffix: 'M' },
    { value: 1_000, suffix: 'k' },
  ]
  for (const { value, suffix } of units) {
    if (n >= value) {
      const scaled = n / value
      let whole = Math.floor(scaled)
      let decimal = Math.round((scaled - whole) * 10)
      if (decimal === 10) { whole += 1; decimal = 0 }
      return decimal === 0 ? `${whole}${suffix}` : `${whole}${suffix}${decimal}`
    }
  }
  return String(n)
}

function HistogramChart({ histogram, threshold, label }) {
  const width = 380
  const height = 110
  const chartHeight = height - 14

  if (!histogram || histogram.buckets.length === 0) {
    return (
      <div style={{ fontSize: 12, color: 'var(--text-muted, #888)', padding: '8px 0' }}>
        {label}: no data in this range.
      </div>
    )
  }

  const { bucketWidth, maxRatio, buckets } = histogram
  const numBins = Math.round(maxRatio / bucketWidth) + 1 // + open-ended overflow bin
  const byBucket = new Map(buckets.map(b => [Math.round(b.bucketStart / bucketWidth), b]))
  const maxCount = Math.max(1, ...buckets.map(b => b.count))
  const barWidth = width / numBins
  // Threshold line uses the SAME per-bucket scale as the bars (width/numBins)
  // rather than width/maxRatio. numBins is one more than the true count of
  // unit bins (it includes the open-ended overflow bin at the end), so those
  // two scales disagree — and increasingly so at higher ratio values, which
  // is why this previously looked roughly right at flux's threshold=2 but
  // visibly off at energy's threshold=6. This keeps the line exactly on its
  // bucket's left edge regardless of where the threshold falls.
  const xForThreshold = (threshold / bucketWidth) * barWidth

  return (
    <div>
      <div style={{ fontSize: 12, color: 'var(--text-muted, #888)', marginBottom: 4 }}>{label}</div>
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" style={{ display: 'block' }}>
        {Array.from({ length: numBins }, (_, i) => {
          const b = byBucket.get(i)
          const count = b?.count ?? 0
          const firedCount = b?.firedCount ?? 0
          const barHeightRaw = (count / maxCount) * chartHeight
          const barHeight = count > 0 ? Math.max(barHeightRaw, MIN_BAR_PX) : 0
          const rawFiredHeight = (firedCount / maxCount) * chartHeight
          const firedHeight = firedCount > 0 ? Math.min(barHeight, Math.max(rawFiredHeight, MIN_FIRED_PX)) : 0
          const x = i * barWidth
          return (
            <g key={i}>
              <rect
                x={x + 1} y={chartHeight - barHeight}
                width={Math.max(0, barWidth - 2)} height={barHeight}
                fill="var(--text-muted, #888)" opacity={0.4}
              />
              {firedCount > 0 && (
                <>
                  <rect
                    x={x + 1} y={chartHeight - firedHeight}
                    width={Math.max(0, barWidth - 2)} height={firedHeight}
                    fill="var(--green, #4caf50)"
                  />
                  <text
                    x={x + barWidth / 2} y={Math.max(9, chartHeight - firedHeight - 3)}
                    fontSize={8} fill="var(--green, #4caf50)" textAnchor="middle"
                  >
                    {formatCompactCount(firedCount)}
                  </text>
                </>
              )}
            </g>
          )
        })}
        <line
          x1={xForThreshold} x2={xForThreshold} y1={0} y2={chartHeight}
          stroke="var(--red, #f44336)" strokeWidth={1} strokeDasharray="3,2"
        />
        <text x={Math.min(xForThreshold + 3, width - 60)} y={10} fontSize={9} fill="var(--red, #f44336)">
          fires ≥ {threshold}
        </text>
        <text x={0} y={height - 2} fontSize={9} fill="var(--text-muted, #888)">0</text>
        <text x={width - 26} y={height - 2} fontSize={9} fill="var(--text-muted, #888)">≥{maxRatio}</text>
      </svg>
    </div>
  )
}

// Downsamples per-minute trigger_event_rollups into a fixed number of
// display bars — a week of per-minute data is 10,000+ points, far more than
// can render as individual bars. First collapses rows sharing the same
// minute (multiple nodes, when no node filter is set) into one combined
// point, so the chart reads as a single timeline rather than interleaved
// per-node bars, then groups consecutive minutes into ROLLUP_TARGET_BARS
// buckets, summing counts within each group.
const ROLLUP_TARGET_BARS = 120

function downsampleRollups(buckets) {
  if (buckets.length === 0) return []
  const byMinute = new Map()
  for (const b of buckets) {
    const cur = byMinute.get(b.bucketStartUs) ?? { bucketStartUs: b.bucketStartUs, entryCount: 0, firedCount: 0 }
    cur.entryCount += b.entryCount
    cur.firedCount += b.firedCount
    byMinute.set(b.bucketStartUs, cur)
  }
  const minutes = [...byMinute.values()].sort((a, b) => a.bucketStartUs - b.bucketStartUs)
  const groupSize = Math.max(1, Math.ceil(minutes.length / ROLLUP_TARGET_BARS))
  const grouped = []
  for (let i = 0; i < minutes.length; i += groupSize) {
    const chunk = minutes.slice(i, i + groupSize)
    grouped.push({
      bucketStartUs: chunk[0].bucketStartUs,
      entryCount: chunk.reduce((s, m) => s + m.entryCount, 0),
      firedCount: chunk.reduce((s, m) => s + m.firedCount, 0),
    })
  }
  return grouped
}

// Time-bucketed activity chart — answers "does this fire sit inside a
// plausible burst of real activity, or isolated with nothing around it",
// which the ratio histogram can't (it collapses time away entirely). Reuses
// the histogram's MIN_BAR_PX/MIN_FIRED_PX visibility floors, but skips the
// histogram's per-bar count labels — at up to 120 bars there's no room for
// them without overlap — and instead prints an exact total underneath,
// computed from the un-downsampled buckets so grouping never distorts it.
function RollupTimeChart({ buckets }) {
  const width = 760
  const height = 110
  const chartHeight = height - 14

  const grouped = downsampleRollups(buckets)
  if (grouped.length === 0) {
    return (
      <div style={{ fontSize: 12, color: 'var(--text-muted, #888)', padding: '8px 0' }}>
        No activity in this range.
      </div>
    )
  }

  const totalEntries = buckets.reduce((s, b) => s + b.entryCount, 0)
  const totalFired = buckets.reduce((s, b) => s + b.firedCount, 0)
  const maxCount = Math.max(1, ...grouped.map(g => g.entryCount))
  const barWidth = width / grouped.length

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" style={{ display: 'block' }}>
        {grouped.map((g, i) => {
          const barHeightRaw = (g.entryCount / maxCount) * chartHeight
          const barHeight = g.entryCount > 0 ? Math.max(barHeightRaw, MIN_BAR_PX) : 0
          const rawFiredHeight = (g.firedCount / maxCount) * chartHeight
          const firedHeight = g.firedCount > 0 ? Math.min(barHeight, Math.max(rawFiredHeight, MIN_FIRED_PX)) : 0
          const x = i * barWidth
          return (
            <g key={g.bucketStartUs}>
              <rect
                x={x + 1} y={chartHeight - barHeight}
                width={Math.max(0, barWidth - 1)} height={barHeight}
                fill="var(--text-muted, #888)" opacity={0.4}
              />
              {g.firedCount > 0 && (
                <rect
                  x={x + 1} y={chartHeight - firedHeight}
                  width={Math.max(0, barWidth - 1)} height={firedHeight}
                  fill="var(--green, #4caf50)"
                />
              )}
            </g>
          )
        })}
      </svg>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, color: 'var(--text-muted, #888)', marginTop: 2 }}>
        <span>{formatDateTime(tUsToIso(grouped[0].bucketStartUs))}</span>
        <span>{formatDateTime(tUsToIso(grouped[grouped.length - 1].bucketStartUs))}</span>
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-muted, #888)', marginTop: 6 }}>
        {formatCompactCount(totalEntries)} interesting blocks · {formatCompactCount(totalFired)} fires in this range
        {grouped.length < buckets.length ? ` (each bar ≈ ${Math.max(1, Math.ceil(
          (grouped[1]?.bucketStartUs - grouped[0]?.bucketStartUs) / 60_000_000 || 1
        ))} min)` : ''}
      </div>
    </div>
  )
}

const STATUS_LABEL = {
  analyzed: 'Analyzed',
  skipped_not_ready: 'Skipped (BirdNET not ready)',
  skipped_birdnet_tdoa_pull: 'Skipped BirdNET (TDOA pull)',
  skipped_tdoa_corroboration: 'Skipped BirdNET (TDOA pull)', // old label, pre-2026-07-11 rows
  error: 'Error',
}

const STATUS_COLOR = {
  analyzed: 'var(--text, #eee)',
  skipped_not_ready: 'var(--yellow, #ffc107)',
  error: 'var(--red, #f44336)',
}

// TDOA orchestration attempt status (tdoa_attempts.status) — see
// species_tdoa_pipeline design, sound-hub/DESIGN.md.
const TDOA_STATUS_LABEL = {
  planned: 'Planned',
  pulling: 'Pulling',
  solved: 'Solved',
  failed: 'Failed',
}
const TDOA_STATUS_COLOR = {
  planned: 'var(--text-muted, #888)',
  pulling: 'var(--yellow, #ffc107)',
  solved: 'var(--green, #4caf50)',
  failed: 'var(--red, #f44336)',
}

// Per-node correlation status (tdoa_attempt_nodes.status) within one attempt.
const TDOA_NODE_STATUS_LABEL = {
  requested: 'Pull requested',
  request_failed: 'Pull failed',
  push_failed: 'Node reported failure',
  reused_existing: 'Reused existing audio',
  origin: 'Origin (trigger)',
  pulled: 'Pulled (direct)',
  arrived: 'Arrived',
  onset_failed: 'Onset detection failed',
}
const TDOA_NODE_STATUS_COLOR = {
  requested: 'var(--text-muted, #888)',
  request_failed: 'var(--red, #f44336)',
  push_failed: 'var(--red, #f44336)',
  reused_existing: 'var(--text-muted, #888)',
  origin: 'var(--text-muted, #888)',
  pulled: 'var(--text-muted, #888)',
  arrived: 'var(--green, #4caf50)',
  onset_failed: 'var(--red, #f44336)',
}

// arrival_us / t_start_us / t_end_us are node-clock epoch microseconds, same
// units as trigger_events.t_us — reuses tUsToIso() below rather than a
// second conversion helper.

export default function AnalyticsTab() {
  const [data, setData]   = useState(null)
  const [error, setError] = useState(null)
  const [nodeFilter, setNodeFilter] = useState('')
  const [triggerData, setTriggerData]   = useState(null)
  const [triggerError, setTriggerError] = useState(null)
  const [histogramData, setHistogramData]   = useState(null)
  const [histogramError, setHistogramError] = useState(null)
  const [rollupData, setRollupData]   = useState(null)
  const [rollupError, setRollupError] = useState(null)
  const [tdoaAttempts, setTdoaAttempts] = useState(null)
  const [tdoaError, setTdoaError]       = useState(null)
  const [openAttemptId, setOpenAttemptId] = useState(null)
  const isMobile = useIsMobile()

  // Moment (Dawn/Dusk/etc.) is still shared with the Detections tab — same
  // sun-relative vocabulary is directly useful here. Date range/Custom is a
  // local fork (TRIGGER_DATE_PRESETS/resolveTriggerRange above), not shared.
  // Defaults to 'today' rather than DetectionsTab's 'all' — an unbounded
  // rollup/histogram query risks a much larger fetch than an unbounded
  // detections query.
  const [datePreset, setDatePreset] = useState('today')
  const [customFrom, setCustomFrom] = useState('')
  const [customTo, setCustomTo]     = useState('')
  const [moment, setMoment]         = useState('')

  function chooseDatePreset(key) {
    setDatePreset(key)
    if (isQuickMoment(moment)) setMoment('')
  }
  function chooseMoment(key) {
    setMoment(key)
  }

  const [activeSubTab, setActiveSubTab] = useState('events') // 'events' | 'trigger'
  const [eventsDetailOpen, setEventsDetailOpen] = useState(false)

  // Push events — same deliberate-query model as the trigger diagnostics
  // fetches below rather than the live 5s poll this used to run: this tab
  // answers "is the trigger firing enough" / "is BirdNET seeing near-misses"
  // over a chosen window, not "watch pushes arrive right now", and a
  // recency-capped live feed has the same crowding problem the trigger
  // detail table did (a busy node buries everything else under its own
  // shared limit) — see project discussion.
  const fetchAnalytics = useCallback(async () => {
    try {
      const range = resolveTriggerRange(moment, datePreset, customFrom, customTo)
      const params = new URLSearchParams({ limit: String(EVENTS_LIMIT) })
      if (nodeFilter) params.set('nodeId', nodeFilter)
      if (range?.from) params.set('from', range.from.toISOString())
      if (range?.to) params.set('to', range.to.toISOString())
      if (moment && !isQuickMoment(moment)) params.set('timeOfDay', moment)
      const res = await apiFetch(`/analytics/audio?${params}`)
      if (!res.ok) {
        const body = await res.json().catch(() => null)
        throw new Error(body?.detail ?? `${res.status}`)
      }
      setData(await res.json())
      setError(null)
    } catch (err) {
      setError(err.message ?? String(err))
    }
  }, [nodeFilter, moment, datePreset, customFrom, customTo])

  // Trigger-diagnostics: per-block dual-gate ratios pulled from each node's
  // /app/api/trigger-diag ring buffer (only near-misses + fires are ever
  // recorded on the node side — see TriggerDiagnostics.hpp). Built to
  // diagnose why the v2 AND-gate trigger stopped firing for some species.
  const fetchTriggerDiag = useCallback(async () => {
    try {
      const params = new URLSearchParams({ limit: String(TRIGGER_SUMMARY_EVENTS_LIMIT) })
      if (nodeFilter) params.set('nodeId', nodeFilter)
      const res = await apiFetch(`/analytics/trigger-diag?${params}`)
      if (!res.ok) {
        const body = await res.json().catch(() => null)
        throw new Error(body?.detail ?? `${res.status}`)
      }
      setTriggerData(await res.json())
      setTriggerError(null)
    } catch (err) {
      setTriggerError(err.message ?? String(err))
    }
  }, [nodeFilter])

  // Ratio histogram: an explicit time-range query, not a live feed — pulling
  // the same range every few seconds would just re-fetch an unchanged
  // result. Refetches only when the range/node selection changes, plus the
  // manual Refresh button below (which also re-runs the other two fetches).
  // Clamped to HISTOGRAM_MAX_LOOKBACK_MS regardless of the selected range,
  // since raw trigger_events can't answer anything older than that anyway.
  // time_of_day ('Dawn' etc.) isn't supported server-side for the histogram
  // yet, so a sun-relative moment selection only narrows the rollup chart
  // below, not this one.
  const fetchHistogram = useCallback(async () => {
    try {
      const range = resolveTriggerRange(moment, datePreset, customFrom, customTo)
      const now = Date.now()
      const earliestMs = now - HISTOGRAM_MAX_LOOKBACK_MS
      const untilMs = range?.to ? Math.min(range.to.getTime(), now) : now
      const sinceMs = Math.max(range?.from ? range.from.getTime() : earliestMs, earliestMs)
      const params = new URLSearchParams({
        sinceUs: String(sinceMs * 1000), untilUs: String(untilMs * 1000),
        bucketWidth: '1', maxRatio: '20',
      })
      if (nodeFilter) params.set('nodeId', nodeFilter)
      const res = await apiFetch(`/analytics/trigger-diag/histogram?${params}`)
      if (!res.ok) {
        const body = await res.json().catch(() => null)
        throw new Error(body?.detail ?? `${res.status}`)
      }
      setHistogramData(await res.json())
      setHistogramError(null)
    } catch (err) {
      setHistogramError(err.message ?? String(err))
    }
  }, [nodeFilter, moment, datePreset, customFrom, customTo])

  // Time-bucketed activity, from trigger_event_rollups — not capped to the
  // raw retention window, so the full selected range applies here (unlike
  // the histogram above). time_of_day is passed straight through for
  // sun-relative moments; quick moments (Last 10 min/Last hour) are already
  // expressed as from/to by resolveTriggerRange.
  const fetchRollups = useCallback(async () => {
    try {
      const range = resolveTriggerRange(moment, datePreset, customFrom, customTo)
      const params = new URLSearchParams()
      if (nodeFilter) params.set('nodeId', nodeFilter)
      if (range?.from) params.set('from', range.from.toISOString())
      if (range?.to) params.set('to', range.to.toISOString())
      if (moment && !isQuickMoment(moment)) params.set('timeOfDay', moment)
      const res = await apiFetch(`/analytics/trigger-diag/rollups?${params}`)
      if (!res.ok) {
        const body = await res.json().catch(() => null)
        throw new Error(body?.detail ?? `${res.status}`)
      }
      setRollupData(await res.json())
      setRollupError(null)
    } catch (err) {
      setRollupError(err.message ?? String(err))
    }
  }, [nodeFilter, moment, datePreset, customFrom, customTo])

  // TDOA attempts — recent orchestration attempts (species_tdoa_pipeline
  // design, sound-hub/DESIGN.md), no date-range filter for now: attempt
  // volume is low enough at this project's scale that "most recent 50" is
  // plenty, unlike the push-event/trigger-diagnostic fetches above which
  // genuinely need a window to stay useful. Not wired to nodeFilter either
  // — an attempt spans multiple nodes, so filtering by a single one doesn't
  // map cleanly onto "which attempts do I want to see".
  const fetchTdoaAttempts = useCallback(async () => {
    try {
      const res = await apiFetch('/tdoa/attempts?limit=50')
      if (!res.ok) {
        const body = await res.json().catch(() => null)
        throw new Error(body?.detail ?? `${res.status}`)
      }
      setTdoaAttempts(await res.json())
      setTdoaError(null)
    } catch (err) {
      setTdoaError(err.message ?? String(err))
    }
  }, [])

  // Downloads a TDOA node's WAV via apiFetch (so the Bearer token goes in
  // the Authorization header, same as every other request) rather than a
  // plain <a href> — this app has no cookie-based session to piggyback on,
  // and putting the token in the URL instead (query param) was explicitly
  // ruled out: browser history and server access logs would carry it.
  // Fetches the file as a blob, then triggers a normal client-side download
  // via a throwaway object URL/anchor.
  const downloadTdoaAudio = useCallback(async (filename) => {
    try {
      const res = await apiFetch(`/tdoa/audio/${encodeURIComponent(filename)}`)
      if (!res.ok) {
        const body = await res.json().catch(() => null)
        throw new Error(body?.detail ?? `${res.status}`)
      }
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (err) {
      setTdoaError(`Could not download ${filename}: ${err.message ?? String(err)}`)
    }
  }, [])

  // No polling on any of these five — this whole tab is a deliberate query
  // over a chosen node/range, not a live-updating feed. Each refetches on
  // mount and whenever its own dependencies (nodeFilter, moment, datePreset,
  // customFrom, customTo) change, plus the single Refresh button below.
  useEffect(() => {
    fetchAnalytics()
  }, [fetchAnalytics])

  useEffect(() => {
    fetchTriggerDiag()
  }, [fetchTriggerDiag])

  useEffect(() => {
    fetchHistogram()
  }, [fetchHistogram])

  useEffect(() => {
    fetchRollups()
  }, [fetchRollups])

  useEffect(() => {
    fetchTdoaAttempts()
  }, [fetchTdoaAttempts])

  const sty = {
    root: { display: 'flex', flexDirection: 'column', gap: 16, padding: 16, overflow: 'auto', flex: 1, minHeight: 0 },
    nodeCol: { fontWeight: 600, color: 'var(--text, #eee)' },
    toolbar: {
      display: 'flex', alignItems: 'center', gap: 8,
    },
    input: {
      background: 'var(--surface2, #2a2a2a)', border: '1px solid var(--border, #333)',
      borderRadius: 4, color: 'var(--text, #eee)', fontSize: 12, padding: '4px 8px',
    },
    tableWrap: {
      background: 'var(--surface1, #1e1e1e)', borderRadius: 8,
      border: '1px solid var(--border, #333)', overflow: 'auto',
    },
    table: { width: '100%', borderCollapse: 'collapse', fontSize: 12 },
    th: {
      textAlign: 'left', padding: '6px 10px', position: 'sticky', top: 0,
      background: 'var(--surface1, #1e1e1e)',
      borderBottom: '1px solid var(--border, #333)',
      color: 'var(--text-muted, #888)', fontWeight: 500, whiteSpace: 'nowrap',
    },
    td: {
      padding: '5px 10px',
      borderBottom: '1px solid var(--border-faint, #2a2a2a)',
      whiteSpace: 'nowrap',
    },
    sectionLabel: { fontSize: 11, color: 'var(--text-muted, #888)', fontWeight: 500, textTransform: 'uppercase', letterSpacing: 0.5 },
    empty: { padding: 24, textAlign: 'center', color: 'var(--text-muted, #888)', fontSize: 13 },
    subTabBar: {
      display: 'flex', gap: 16, borderBottom: '1px solid var(--border, #333)',
    },
    subTabBtn: {
      background: 'none', border: 'none', borderBottom: '2px solid transparent',
      color: 'var(--text-muted, #888)', fontSize: 13, fontWeight: 500,
      padding: '6px 2px', cursor: 'pointer',
    },
    subTabBtnActive: {
      color: 'var(--text, #eee)', borderBottom: '2px solid var(--text, #eee)',
    },
    disclosureBtn: {
      display: 'flex', alignItems: 'center', gap: 6,
      background: 'none', border: 'none', cursor: 'pointer',
      color: 'var(--text-muted, #888)', fontSize: 12, padding: '4px 0',
    },
    detailWrap: {
      background: 'var(--surface1, #1e1e1e)', borderRadius: 8,
      border: '1px solid var(--border, #333)', overflowY: 'auto', maxHeight: 260,
      flexShrink: 0,
    },
    presetBtn: (active) => ({
      padding: '4px 10px', fontSize: 12, borderRadius: 14,
      border: `1px solid ${active ? 'var(--accent, #4da6ff)' : 'var(--border, #333)'}`,
      background: active ? 'var(--accent, #4da6ff)' : 'transparent',
      color: active ? '#fff' : 'var(--text-muted, #888)',
      cursor: 'pointer', whiteSpace: 'nowrap',
    }),
  }

  if (error) {
    return (
      <div style={{ ...sty.root }}>
        <div style={{
          fontSize: 12, padding: '6px 10px', borderRadius: 4,
          background: 'rgba(244,67,54,0.12)', color: 'var(--red, #f44336)',
        }}>
          {error}
        </div>
      </div>
    )
  }

  if (!data) {
    return <div style={sty.root}><div style={sty.empty}>Loading…</div></div>
  }

  const { summary, events } = data

  return (
    <div style={sty.root}>
      {/* Shared across both sub-tabs — same window applies whether you're
          looking at pushes or trigger diagnostics, since they're usually the
          same investigation from two angles. Date range/Custom is a local
          fork (TRIGGER_DATE_PRESETS above); Moment stays shared with the
          Detections tab's sun-relative vocabulary. */}
      <div style={{ ...sty.toolbar, gap: 8, flexWrap: 'wrap' }}>
        {TRIGGER_DATE_PRESETS.map(p => (
          <button key={p.key} style={sty.presetBtn(datePreset === p.key)} onClick={() => chooseDatePreset(p.key)}>
            {p.label}
          </button>
        ))}
        {datePreset === 'custom' && (
          <>
            <input type="datetime-local" value={customFrom} onChange={e => setCustomFrom(e.target.value)} style={sty.input} />
            <span style={{ color: 'var(--text-muted, #888)', fontSize: 12 }}>to</span>
            <input type="datetime-local" value={customTo} onChange={e => setCustomTo(e.target.value)} style={sty.input} />
          </>
        )}
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {MOMENT_OPTIONS.map(o => (
          <button key={o.key || 'all-day'} style={sty.presetBtn(moment === o.key)} onClick={() => chooseMoment(o.key)}>
            {o.label}
          </button>
        ))}
      </div>
      <div style={sty.toolbar}>
        <input
          style={sty.input}
          placeholder="Filter by node id…"
          value={nodeFilter}
          onChange={e => setNodeFilter(e.target.value)}
        />
        <div style={{ flex: 1 }} />
        <button
          style={{
            ...sty.disclosureBtn, padding: '4px 10px',
            border: '1px solid var(--border, #333)', borderRadius: 4,
          }}
          onClick={() => { fetchAnalytics(); fetchTriggerDiag(); fetchHistogram(); fetchRollups(); fetchTdoaAttempts() }}
        >
          Refresh
        </button>
      </div>

      <div style={sty.subTabBar}>
        <button
          style={{ ...sty.subTabBtn, ...(activeSubTab === 'events' ? sty.subTabBtnActive : {}) }}
          onClick={() => setActiveSubTab('events')}
        >
          Push events
        </button>
        <button
          style={{ ...sty.subTabBtn, ...(activeSubTab === 'trigger' ? sty.subTabBtnActive : {}) }}
          onClick={() => setActiveSubTab('trigger')}
        >
          Trigger diagnostics
        </button>
        <button
          style={{ ...sty.subTabBtn, ...(activeSubTab === 'tdoa' ? sty.subTabBtnActive : {}) }}
          onClick={() => setActiveSubTab('tdoa')}
        >
          Localisation
        </button>
      </div>

      {activeSubTab === 'events' && (
        <>
          <div>
            <div style={{ ...sty.sectionLabel, marginBottom: 8 }}>Per-node summary</div>
            {summary.length === 0 ? (
              <div style={sty.empty}>No audio pushes recorded yet.</div>
            ) : (
              <div style={sty.tableWrap}>
                <table style={sty.table}>
                  <thead>
                    <tr>
                      <th style={sty.th}>Node</th>
                      <th style={sty.th}>Total pushes</th>
                      <th style={sty.th}>Self-triggered</th>
                      <th style={sty.th}>With detection</th>
                      <th style={sty.th}>Zero detections</th>
                      <th style={sty.th}>Avg near-miss conf.</th>
                      <th style={sty.th}>Last push</th>
                      <th style={sty.th}>Last self-trigger</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.map(s => {
                      const zeroPct = s.totalPushes ? Math.round((s.pushesZeroDetections / s.totalPushes) * 100) : 0
                      return (
                        <tr key={s.nodeId ?? 'unknown'}>
                          <td style={{ ...sty.td, ...sty.nodeCol }}>{s.nodeId ?? '(unknown node)'}</td>
                          <td style={sty.td}>{s.totalPushes}</td>
                          <td style={sty.td}>{s.triggeredPushes}</td>
                          <td style={sty.td}>{s.pushesWithDetections}</td>
                          <td style={sty.td}>{s.pushesZeroDetections} ({zeroPct}%)</td>
                          <td style={sty.td}>
                            {s.avgNearMissConfidence != null ? `${Math.round(s.avgNearMissConfidence * 100)}%` : '—'}
                          </td>
                          <td style={sty.td}>{relativeTime(s.lastPushAt)}</td>
                          <td style={sty.td}>{relativeTime(s.lastTriggerAt)}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div style={sty.toolbar}>
            <button style={sty.disclosureBtn} onClick={() => setEventsDetailOpen(o => !o)}>
              <span>{eventsDetailOpen ? '▾' : '▸'}</span>
              Recent push events ({events.length})
            </button>
          </div>

          {eventsDetailOpen && (
            <div style={sty.detailWrap}>
              {events.length === 0 ? (
                <div style={sty.empty}>No push events match the current filter.</div>
              ) : (
                <table style={sty.table}>
                  <thead>
                    <tr>
                      <th style={sty.th}>{isMobile ? 'Time' : 'Received'}</th>
                      <th style={sty.th}>Node</th>
                      <th style={sty.th}>Source</th>
                      <th style={sty.th}>Status</th>
                      <th style={sty.th}>Bytes</th>
                      <th style={sty.th}>Detections</th>
                      <th style={sty.th}>Top candidate</th>
                    </tr>
                  </thead>
                  <tbody>
                    {events.map(e => (
                      <tr key={e.id}>
                        <td style={sty.td}>{isMobile ? formatTime(e.receivedAt) : formatDateTime(e.receivedAt)}</td>
                        <td style={sty.td}>{e.nodeId ?? '—'}</td>
                        <td style={sty.td}>{e.triggered ? 'Self-trigger' : 'Hub pull'}</td>
                        <td style={{ ...sty.td, color: STATUS_COLOR[e.analysisStatus] ?? sty.td.color }}>
                          {STATUS_LABEL[e.analysisStatus] ?? e.analysisStatus}
                        </td>
                        <td style={sty.td}>{e.bytes.toLocaleString()}</td>
                        <td style={sty.td}>{e.detectionCount}</td>
                        <td style={{ ...sty.td, color: 'var(--text-muted, #888)' }}>
                          {e.topSpecies ? `${e.topSpecies} (${Math.round((e.topConfidence ?? 0) * 100)}%)` : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </>
      )}

      {activeSubTab === 'trigger' && (
        <>
          <div>
            <div style={{ ...sty.sectionLabel, marginBottom: 8 }}>Trigger diagnostics summary (v2 dual-gate)</div>
            {triggerError ? (
              <div style={{
                fontSize: 12, padding: '6px 10px', borderRadius: 4,
                background: 'rgba(244,67,54,0.12)', color: 'var(--red, #f44336)',
              }}>
                {triggerError}
              </div>
            ) : !triggerData ? (
              <div style={sty.empty}>Loading…</div>
            ) : triggerData.summary.length === 0 ? (
              <div style={sty.empty}>No trigger-diagnostic rows recorded yet.</div>
            ) : (
              <div style={sty.tableWrap}>
                <table style={sty.table}>
                  <thead>
                    <tr>
                      <th style={sty.th}>Node</th>
                      <th style={sty.th}>Rows (recent)</th>
                      <th style={sty.th}>Fired</th>
                      <th style={sty.th}>Near-misses</th>
                      <th style={sty.th}>Avg energy ratio</th>
                      <th style={sty.th}>Avg flux ratio</th>
                      <th style={sty.th}>Last row</th>
                    </tr>
                  </thead>
                  <tbody>
                    {triggerData.summary.map(s => (
                      <tr key={s.nodeId ?? 'unknown'}>
                        <td style={{ ...sty.td, ...sty.nodeCol }}>{s.nodeId ?? '(unknown node)'}</td>
                        <td style={sty.td}>{s.totalRows}</td>
                        <td style={sty.td}>{s.firedRows}</td>
                        <td style={sty.td}>{s.nearMissRows}</td>
                        <td style={sty.td}>{s.avgEnergyRatio != null ? s.avgEnergyRatio.toFixed(2) : '—'}</td>
                        <td style={sty.td}>{s.avgFluxRatio != null ? s.avgFluxRatio.toFixed(2) : '—'}</td>
                        <td style={sty.td}>{relativeTime(tUsToIso(s.lastTUs))}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {triggerData && triggerData.summary.length > 0 && (
            <div>
              <div style={{ ...sty.sectionLabel, marginBottom: 8 }}>Activity over time</div>
              {rollupError ? (
                <div style={{
                  fontSize: 12, padding: '6px 10px', borderRadius: 4,
                  background: 'rgba(244,67,54,0.12)', color: 'var(--red, #f44336)',
                }}>
                  {rollupError}
                </div>
              ) : !rollupData ? (
                <div style={sty.empty}>Loading…</div>
              ) : (
                <div style={{
                  background: 'var(--surface1, #1e1e1e)', borderRadius: 8,
                  border: '1px solid var(--border, #333)', padding: 12, marginBottom: 20,
                }}>
                  <RollupTimeChart buckets={rollupData.buckets} />
                  <div style={{ fontSize: 11, color: 'var(--text-muted, #888)', marginTop: 8 }}>
                    Muted = near-miss + fired blocks per bar; green = the fired portion (floored to
                    stay visible). From trigger_event_rollups, which is never pruned — unlike the
                    histogram below, the full selected range applies here, including "Dawn"/"Dusk".
                    A fire with no surrounding muted activity is worth a second look; one that sits
                    inside a rising bar is consistent with real, ongoing activity.
                  </div>
                </div>
              )}

              <div style={{ ...sty.sectionLabel, marginBottom: 8 }}>Ratio histogram</div>
              {histogramError ? (
                <div style={{
                  fontSize: 12, padding: '6px 10px', borderRadius: 4,
                  background: 'rgba(244,67,54,0.12)', color: 'var(--red, #f44336)',
                }}>
                  {histogramError}
                </div>
              ) : !histogramData ? (
                <div style={sty.empty}>Loading…</div>
              ) : (
                <div style={{
                  background: 'var(--surface1, #1e1e1e)', borderRadius: 8,
                  border: '1px solid var(--border, #333)', padding: 12,
                  display: 'flex', flexDirection: 'column', gap: 16,
                }}>
                  <HistogramChart histogram={histogramData.energy} threshold={6.0} label="Energy ratio" />
                  <HistogramChart histogram={histogramData.flux} threshold={2.0} label="Flux ratio" />
                  <div style={{ fontSize: 11, color: 'var(--text-muted, #888)' }}>
                    Muted bars = near-miss + fired blocks per bucket; green = the fired portion,
                    height floored to stay visible and labelled with the exact count (fires are
                    routinely 1000:1 or rarer against near-misses in the same bucket). Green below
                    the dashed threshold line is a fire attributable to the low-band gate — its
                    ratios here are whatever the high-band gates read at that moment, not what
                    triggered it (the node doesn't currently record which gate fired). Scoped to
                    the raw retention window (recent hours) regardless of the range selected above
                    — see the time chart for anything older.
                  </div>
                </div>
              )}
            </div>
          )}
        </>
      )}

      {activeSubTab === 'tdoa' && (
        <div>
          <div style={{ ...sty.sectionLabel, marginBottom: 8 }}>
            Recent TDOA attempts
          </div>
          {tdoaError ? (
            <div style={{
              fontSize: 12, padding: '6px 10px', borderRadius: 4,
              background: 'rgba(244,67,54,0.12)', color: 'var(--red, #f44336)',
            }}>
              {tdoaError}
            </div>
          ) : !tdoaAttempts ? (
            <div style={sty.empty}>Loading…</div>
          ) : tdoaAttempts.length === 0 ? (
            <div style={sty.empty}>No TDOA attempts recorded yet.</div>
          ) : (
            <div style={sty.tableWrap}>
              <table style={sty.table}>
                <thead>
                  <tr>
                    <th style={sty.th}></th>
                    <th style={sty.th}>{isMobile ? 'Time' : 'Created'}</th>
                    <th style={sty.th}>Species</th>
                    <th style={sty.th}>Status</th>
                    <th style={sty.th}>Nodes arrived</th>
                    <th style={sty.th}>Result</th>
                  </tr>
                </thead>
                <tbody>
                  {tdoaAttempts.map(a => {
                    const arrivedCount = a.nodes.filter(n => n.status === 'arrived').length
                    const isOpen = openAttemptId === a.id
                    const mainRow = (
                      <tr
                        key={`${a.id}-main`}
                        style={{ cursor: 'pointer' }}
                        onClick={() => setOpenAttemptId(o => (o === a.id ? null : a.id))}
                      >
                        <td style={sty.td}>{isOpen ? '▾' : '▸'}</td>
                        <td style={sty.td}>{isMobile ? formatTime(a.createdAt) : formatDateTime(a.createdAt)}</td>
                        <td style={sty.td}>
                          {a.speciesKey}
                          {a.usedDefault && (
                            <span
                              style={{ color: 'var(--text-muted, #888)', fontSize: 10, marginLeft: 4 }}
                              title="No species-specific TDOA params configured — used the __default__ fallback"
                            >
                              (default params)
                            </span>
                          )}
                        </td>
                        <td style={{ ...sty.td, color: TDOA_STATUS_COLOR[a.status] ?? sty.td.color }}>
                          {TDOA_STATUS_LABEL[a.status] ?? a.status}
                        </td>
                        <td style={sty.td}>{arrivedCount}/{a.minCorroboratingNodes}</td>
                        <td style={{ ...sty.td, color: 'var(--text-muted, #888)' }}>
                          {a.status === 'solved'
                            ? `E ${a.solvedE.toFixed(2)} N ${a.solvedN.toFixed(2)} Alt ${a.solvedAlt.toFixed(2)} (±${a.solveResidualM.toFixed(2)}m, ${a.solveMethod})`
                            : a.status === 'failed'
                              ? (a.failureReason ?? '—')
                              : '—'}
                        </td>
                      </tr>
                    )
                    if (!isOpen) return mainRow
                    return [
                      mainRow,
                      <tr key={`${a.id}-detail`}>
                        <td colSpan={6} style={{ padding: 0, borderBottom: '1px solid var(--border-faint, #2a2a2a)' }}>
                          <div style={{ padding: '8px 10px 12px 30px', background: 'var(--surface1, #1e1e1e)' }}>
                            {a.status === 'solved' && a.solveAmbiguousRoot && (
                              <div style={{ fontSize: 11, color: 'var(--yellow, #ffc107)', marginBottom: 8 }}>
                                4-node solve — mirror root also mathematically valid: E {a.solveAmbiguousRoot[0].toFixed(2)} N{' '}
                                {a.solveAmbiguousRoot[1].toFixed(2)} Alt {a.solveAmbiguousRoot[2].toFixed(2)} (not
                                auto-resolved — no hint point configured; set min_corroborating_nodes=5 for an
                                unambiguous solve instead)
                              </div>
                            )}
                            <table style={sty.table}>
                              <thead>
                                <tr>
                                  <th style={sty.th}>Node</th>
                                  <th style={sty.th}>Role</th>
                                  <th style={sty.th}>Arrival</th>
                                  <th style={sty.th}>File</th>
                                  <th style={sty.th}>Error</th>
                                </tr>
                              </thead>
                              <tbody>
                                {a.nodes.map(n => (
                                  <tr key={n.id}>
                                    <td style={{ ...sty.td, ...sty.nodeCol }}>{n.nodeId}</td>
                                    <td style={{ ...sty.td, color: TDOA_NODE_STATUS_COLOR[n.status] ?? sty.td.color }}>
                                      {TDOA_NODE_STATUS_LABEL[n.status] ?? n.status}
                                    </td>
                                    <td style={sty.td}>
                                      {n.arrivalUs != null ? formatDateTime(tUsToIso(n.arrivalUs)) : '—'}
                                    </td>
                                    <td style={{ ...sty.td, fontFamily: 'monospace', fontSize: 11 }}>
                                      {n.filename ? (
                                        <button
                                          onClick={() => downloadTdoaAudio(n.filename)}
                                          title="Download this WAV"
                                          style={{
                                            background: 'none', border: 'none', padding: 0,
                                            color: 'var(--accent, #4da6ff)', textDecoration: 'underline',
                                            cursor: 'pointer', fontFamily: 'monospace', fontSize: 11,
                                          }}
                                        >
                                          {n.filename}
                                        </button>
                                      ) : '—'}
                                    </td>
                                    <td style={{
                                      ...sty.td, color: 'var(--red, #f44336)',
                                      whiteSpace: 'normal', minWidth: 260, maxWidth: 420,
                                    }}>
                                      {n.error ?? '—'}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        </td>
                      </tr>,
                    ]
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
