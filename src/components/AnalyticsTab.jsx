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

function ratioColour(ratio) {
  if (ratio >= 6.0) return 'var(--green, #4caf50)'
  if (ratio >= 3.0) return 'var(--yellow, #ffc107)'
  return 'var(--text-muted, #888)'
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
  const isMobile = useIsMobile()

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
      const params = new URLSearchParams({ limit: String(EVENTS_LIMIT) })
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

  useEffect(() => {
    fetchAnalytics()
    const t = setInterval(fetchAnalytics, POLL_INTERVAL_MS)
    return () => clearInterval(t)
  }, [fetchAnalytics])

  useEffect(() => {
    fetchTriggerDiag()
    const t = setInterval(fetchTriggerDiag, POLL_INTERVAL_MS)
    return () => clearInterval(t)
  }, [fetchTriggerDiag])

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
        <div style={sty.sectionLabel}>Recent push events</div>
        <div style={{ flex: 1 }} />
        <input
          style={sty.input}
          placeholder="Filter by node id…"
          value={nodeFilter}
          onChange={e => setNodeFilter(e.target.value)}
        />
      </div>

      <div style={sty.tableWrap}>
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

      <div>
        <div style={{ ...sty.sectionLabel, marginBottom: 8 }}>Trigger diagnostics (v2 dual-gate)</div>
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

      {triggerData && (
        <div style={sty.tableWrap}>
          {triggerData.events.length === 0 ? (
            <div style={sty.empty}>No trigger-diagnostic blocks match the current filter.</div>
          ) : (
            <table style={sty.table}>
              <thead>
                <tr>
                  <th style={sty.th}>{isMobile ? 'Time' : 'Block time'}</th>
                  <th style={sty.th}>Node</th>
                  <th style={sty.th}>Energy ratio</th>
                  <th style={sty.th}>Flux ratio</th>
                  <th style={sty.th}>Fired</th>
                </tr>
              </thead>
              <tbody>
                {triggerData.events.map(e => (
                  <tr key={e.id}>
                    <td style={sty.td}>
                      {isMobile ? formatTime(tUsToIso(e.tUs)) : formatDateTime(tUsToIso(e.tUs))}
                    </td>
                    <td style={sty.td}>{e.nodeId ?? '—'}</td>
                    <td style={{ ...sty.td, color: ratioColour(e.energyRatio) }}>{e.energyRatio.toFixed(2)}</td>
                    <td style={{ ...sty.td, color: ratioColour(e.fluxRatio) }}>{e.fluxRatio.toFixed(2)}</td>
                    <td style={{ ...sty.td, color: e.fired ? 'var(--green, #4caf50)' : sty.td.color }}>
                      {e.fired ? 'Fired' : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  )
}
