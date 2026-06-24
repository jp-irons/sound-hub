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

  useEffect(() => {
    fetchAnalytics()
    const t = setInterval(fetchAnalytics, POLL_INTERVAL_MS)
    return () => clearInterval(t)
  }, [fetchAnalytics])

  const sty = {
    root: { display: 'flex', flexDirection: 'column', gap: 16, padding: 16, overflow: 'auto', flex: 1 },
    cards: { display: 'flex', flexWrap: 'wrap', gap: 12 },
    card: {
      flex: '1 1 220px', minWidth: 200,
      background: 'var(--surface1, #1e1e1e)', border: '1px solid var(--border, #333)',
      borderRadius: 8, padding: '12px 14px',
    },
    cardNode: { fontWeight: 600, fontSize: 13, marginBottom: 8 },
    statRow: {
      display: 'flex', justifyContent: 'space-between', fontSize: 12,
      padding: '3px 0', color: 'var(--text-muted, #888)',
    },
    statVal: { color: 'var(--text, #eee)', fontWeight: 500 },
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
          <div style={sty.cards}>
            {summary.map(s => {
              const zeroPct = s.totalPushes ? Math.round((s.pushesZeroDetections / s.totalPushes) * 100) : 0
              return (
                <div key={s.nodeId ?? 'unknown'} style={sty.card}>
                  <div style={sty.cardNode}>{s.nodeId ?? '(unknown node)'}</div>
                  <div style={sty.statRow}><span>Total pushes</span><span style={sty.statVal}>{s.totalPushes}</span></div>
                  <div style={sty.statRow}><span>Self-triggered</span><span style={sty.statVal}>{s.triggeredPushes}</span></div>
                  <div style={sty.statRow}><span>With detection</span><span style={sty.statVal}>{s.pushesWithDetections}</span></div>
                  <div style={sty.statRow}><span>Zero detections</span><span style={sty.statVal}>{s.pushesZeroDetections} ({zeroPct}%)</span></div>
                  <div style={sty.statRow}>
                    <span>Avg near-miss conf.</span>
                    <span style={sty.statVal}>
                      {s.avgNearMissConfidence != null ? `${Math.round(s.avgNearMissConfidence * 100)}%` : '—'}
                    </span>
                  </div>
                  <div style={sty.statRow}><span>Last push</span><span style={sty.statVal}>{relativeTime(s.lastPushAt)}</span></div>
                  <div style={sty.statRow}><span>Last self-trigger</span><span style={sty.statVal}>{relativeTime(s.lastTriggerAt)}</span></div>
                </div>
              )
            })}
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
    </div>
  )
}
