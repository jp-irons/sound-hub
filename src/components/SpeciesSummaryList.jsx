import { useState, useEffect, useCallback } from 'react'
import { buildDetectionParams } from '../utils/detectionFilters.js'
import { ConfBar, formatTime, formatDateTime } from './DetectionFormat.jsx'

const API_BASE = '/api'
const POLL_INTERVAL_MS = 5000
const SUMMARY_LIMIT = 500   // species-summary rows are already aggregated server-side
const DETAIL_LIMIT = 100    // per-species detail rows shown on expand

export default function SpeciesSummaryList({ minConf, species, datePreset, customFrom, customTo, timeOfDay }) {
  const [summary, setSummary]   = useState([])
  const [error, setError]       = useState(null)
  const [expanded, setExpanded] = useState(() => new Set())
  const [details, setDetails]   = useState({})       // commonName -> rows | 'loading' | 'error'

  const fetchSummary = useCallback(async () => {
    try {
      const params = buildDetectionParams({
        minConf, species, datePreset, customFrom, customTo, timeOfDay, limit: SUMMARY_LIMIT,
      })
      const res = await fetch(`${API_BASE}/detections/species-summary?${params}`)
      if (!res.ok) {
        const body = await res.json().catch(() => null)
        throw new Error(body?.detail ?? `${res.status}`)
      }
      setSummary(await res.json())
      setError(null)
    } catch (err) {
      setError(err.message ?? String(err))
    }
  }, [minConf, species, datePreset, customFrom, customTo, timeOfDay])

  useEffect(() => {
    fetchSummary()
    const t = setInterval(fetchSummary, POLL_INTERVAL_MS)
    return () => clearInterval(t)
  }, [fetchSummary])

  // Species no longer in the summary (e.g. filters changed) shouldn't keep
  // stale expanded/detail state around indefinitely, but it's harmless to
  // leave it — re-expanding re-fetches. Not worth pruning for v1.

  async function toggleExpand(commonName) {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(commonName)) next.delete(commonName)
      else next.add(commonName)
      return next
    })

    if (details[commonName]) return // already fetched (or in flight/error)

    setDetails(prev => ({ ...prev, [commonName]: 'loading' }))
    try {
      // species= is a substring match server-side (matches either name
      // field) — filter the result to an exact commonName match client-side
      // so a row never shows another species' detections (e.g. two species
      // sharing a common-name substring).
      const params = buildDetectionParams({
        minConf, species: commonName, datePreset, customFrom, customTo, timeOfDay, limit: DETAIL_LIMIT,
      })
      const res = await fetch(`${API_BASE}/detections?${params}`)
      if (!res.ok) throw new Error(`${res.status}`)
      const rows = await res.json()
      const exact = rows.filter(r => r.commonName === commonName)
      setDetails(prev => ({ ...prev, [commonName]: exact }))
    } catch {
      setDetails(prev => ({ ...prev, [commonName]: 'error' }))
    }
  }

  const sty = {
    root: {
      flex: 1, overflow: 'auto', background: 'var(--surface1, #1e1e1e)',
      borderRadius: 8, border: '1px solid var(--border, #333)',
    },
    row: {
      display: 'flex', alignItems: 'center', gap: 12,
      padding: '8px 12px', cursor: 'pointer',
      borderBottom: '1px solid var(--border-faint, #2a2a2a)',
    },
    chevron: (open) => ({
      display: 'inline-block', width: 12, fontSize: 11,
      color: 'var(--text-muted, #888)',
      transform: open ? 'rotate(90deg)' : 'rotate(0deg)',
      transition: 'transform 0.15s',
    }),
    name: { fontWeight: 500, fontSize: 13, flex: 1, minWidth: 0 },
    sci: { fontStyle: 'italic', color: 'var(--text-muted, #888)', fontSize: 12 },
    count: {
      fontSize: 12, color: 'var(--text-muted, #888)',
      minWidth: 70, textAlign: 'right',
    },
    lastSeen: {
      fontSize: 12, color: 'var(--text-muted, #888)',
      minWidth: 110, textAlign: 'right',
    },
    detailWrap: { padding: '0 12px 10px 34px' },
    table: { width: '100%', borderCollapse: 'collapse', fontSize: 12 },
    th: {
      textAlign: 'left', padding: '4px 8px',
      borderBottom: '1px solid var(--border, #333)',
      color: 'var(--text-muted, #888)', fontWeight: 500,
    },
    td: {
      padding: '4px 8px',
      borderBottom: '1px solid var(--border-faint, #2a2a2a)',
      color: 'var(--text, #eee)',
    },
  }

  if (error) {
    return (
      <div style={{
        fontSize: 12, padding: '6px 10px', borderRadius: 4,
        background: 'rgba(244,67,54,0.12)', color: 'var(--red, #f44336)',
      }}>
        {error}
      </div>
    )
  }

  if (summary.length === 0) {
    return (
      <div style={sty.root}>
        <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted, #888)', fontSize: 13 }}>
          No detections match the current filters.
        </div>
      </div>
    )
  }

  return (
    <div style={sty.root}>
      {summary.map(s => {
        const open = expanded.has(s.commonName)
        const detail = details[s.commonName]
        return (
          <div key={s.commonName}>
            <div style={sty.row} onClick={() => toggleExpand(s.commonName)}>
              <span style={sty.chevron(open)}>▶</span>
              <span style={sty.name}>{s.commonName}</span>
              <span style={sty.sci}>{s.scientificName}</span>
              <span style={sty.count}>{s.count} detection{s.count !== 1 ? 's' : ''}</span>
              <span style={sty.lastSeen}>{formatDateTime(s.lastSeen)}</span>
            </div>
            {open && (
              <div style={sty.detailWrap}>
                {detail === 'loading' && (
                  <div style={{ padding: '8px 0', color: 'var(--text-muted, #888)', fontSize: 12 }}>Loading…</div>
                )}
                {detail === 'error' && (
                  <div style={{ padding: '8px 0', color: 'var(--red, #f44336)', fontSize: 12 }}>Failed to load detections.</div>
                )}
                {Array.isArray(detail) && (
                  <table style={sty.table}>
                    <thead>
                      <tr>
                        <th style={sty.th}>Time</th>
                        <th style={sty.th}>Confidence</th>
                        <th style={sty.th}>Source</th>
                        <th style={sty.th}>Offset</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.map(d => (
                        <tr key={d.id}>
                          <td style={sty.td}>{formatTime(d.analyzedAt)}</td>
                          <td style={sty.td}><ConfBar value={d.confidence} /></td>
                          <td style={{ ...sty.td, color: 'var(--text-muted, #888)' }}>{d.source ?? '—'}</td>
                          <td style={{ ...sty.td, color: 'var(--text-muted, #888)' }}>
                            {d.startSec != null ? `${d.startSec}–${d.endSec}s` : '—'}
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
      })}
    </div>
  )
}
