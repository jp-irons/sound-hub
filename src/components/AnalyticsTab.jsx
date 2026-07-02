// Audio pipeline analytics — visibility into every push to POST /api/audio/push,
// regardless of BirdNET outcome. Pairs with DetectionsTab (which only ever shows
// the hits): this tab is for answering "is the node trigger firing enough?" and
// "is BirdNET seeing candidates that just fall below threshold?"
import { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '../auth.js'
import { formatTime, formatDateTime } from './DetectionFormat.jsx'
import { useIsMobile } from '../hooks/useBreakpoint.js'

const POLL_INTERVAL_MS = 5000
const EVENTS_LIMIT = 200

// Trigger-diagnostics summary fetch no longer needs the raw `events` array
// (the per-block detail table it fed was replaced by the ratio histogram
// below — a sustained call flooded that table with near-identical near-miss
// rows and buried the one row that mattered, see project discussion). Ask
// for the minimum the backend will accept rather than paying for a payload
// nothing renders.
const TRIGGER_SUMMARY_EVENTS_LIMIT = 1

// Histogram time-range presets. Capped at 6h because /trigger-diag/histogram
// reads raw trigger_events, which only survives TRIGGER_EVENTS_RETENTION_HOURS
// (6h server-side) before being pruned down to per-minute rollups that can't
// reconstruct a distribution.
const RANGE_PRESETS = [
  { key: '15m', label: 'Last 15 min', ms: 15 * 60 * 1000 },
  { key: '1h', label: 'Last hour', ms: 60 * 60 * 1000 },
  { key: '6h', label: 'Last 6h (max)', ms: 6 * 60 * 60 * 1000 },
]

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

const STATUS_LABEL = {
  analyzed: 'Analyzed',
  skipped_not_ready: 'Skipped (BirdNET not ready)',
  error: 'Error',
}

const STATUS_COLOR = {
  analyzed: 'var(--text, #eee)',
  skipped_not_ready: 'var(--yellow, #ffc107)',
  error: 'var(--red, #f44336)',
}

export default function AnalyticsTab() {
  const [data, setData]   = useState(null)
  const [error, setError] = useState(null)
  const [nodeFilter, setNodeFilter] = useState('')
  const [triggerData, setTriggerData]   = useState(null)
  const [triggerError, setTriggerError] = useState(null)
  const [histogramData, setHistogramData]   = useState(null)
  const [histogramError, setHistogramError] = useState(null)
  const [rangePreset, setRangePreset] = useState('1h')
  const isMobile = useIsMobile()

  const [activeSubTab, setActiveSubTab] = useState('events') // 'events' | 'trigger'
  const [eventsDetailOpen, setEventsDetailOpen] = useState(false)

  const fetchAnalytics = useCallback(async () => {
    try {
      const params = new URLSearchParams({ limit: String(EVENTS_LIMIT) })
      if (nodeFilter) params.set('nodeId', nodeFilter)
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
  }, [nodeFilter])

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
  // manual Refresh button below (which also re-runs fetchTriggerDiag).
  const fetchHistogram = useCallback(async () => {
    try {
      const preset = RANGE_PRESETS.find(p => p.key === rangePreset) ?? RANGE_PRESETS[1]
      const untilUs = Date.now() * 1000
      const sinceUs = untilUs - preset.ms * 1000
      const params = new URLSearchParams({
        sinceUs: String(sinceUs), untilUs: String(untilUs),
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
  }, [nodeFilter, rangePreset])

  useEffect(() => {
    fetchAnalytics()
    const t = setInterval(fetchAnalytics, POLL_INTERVAL_MS)
    return () => clearInterval(t)
  }, [fetchAnalytics])

  // No polling here (unlike fetchAnalytics above) — this tab is a deliberate
  // query over a chosen node/range, not a live-updating feed. Refetches on
  // mount and whenever fetchTriggerDiag/fetchHistogram's own dependencies
  // (nodeFilter, rangePreset) change, plus the manual Refresh button.
  useEffect(() => {
    fetchTriggerDiag()
  }, [fetchTriggerDiag])

  useEffect(() => {
    fetchHistogram()
  }, [fetchHistogram])

  const sty = {
    root: { display: 'flex', flexDirection: 'column', gap: 16, padding: 16, overflow: 'auto', flex: 1 },
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
    },
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
            <div style={{ flex: 1 }} />
            <input
              style={sty.input}
              placeholder="Filter by node id…"
              value={nodeFilter}
              onChange={e => setNodeFilter(e.target.value)}
            />
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
              <div style={{ ...sty.toolbar, marginBottom: 8 }}>
                <div style={sty.sectionLabel}>Ratio histogram</div>
                <div style={{ flex: 1 }} />
                <select
                  style={sty.input}
                  value={rangePreset}
                  onChange={e => setRangePreset(e.target.value)}
                >
                  {RANGE_PRESETS.map(p => (
                    <option key={p.key} value={p.key}>{p.label}</option>
                  ))}
                </select>
                <input
                  style={sty.input}
                  placeholder="Filter by node id…"
                  value={nodeFilter}
                  onChange={e => setNodeFilter(e.target.value)}
                />
                <button
                  style={{
                    ...sty.disclosureBtn, padding: '4px 10px',
                    border: '1px solid var(--border, #333)', borderRadius: 4,
                  }}
                  onClick={() => { fetchTriggerDiag(); fetchHistogram() }}
                >
                  Refresh
                </button>
              </div>

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
                    the raw retention window (recent hours) — older activity only survives in
                    per-minute rollups, not shown here yet.
                  </div>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}
