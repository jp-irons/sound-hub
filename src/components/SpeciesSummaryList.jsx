import { useState, useEffect, useCallback } from 'react'
import { buildDetectionParams } from '../utils/detectionFilters.js'
import { ConfBar, formatTime, formatDateTime } from './DetectionFormat.jsx'

const API_BASE = '/api'
const POLL_INTERVAL_MS = 5000
const SUMMARY_LIMIT = 500   // species-summary rows are already aggregated server-side
const DETAIL_LIMIT = 100    // per-species detail rows shown on expand

const PINNED_KEY = 'detections.pinnedSpecies'
const SORT_KEY = 'detections.sortMode'

const SORT_OPTIONS = [
  { key: 'count_desc', label: 'Most frequent' },
  { key: 'count_asc',  label: 'Least frequent' },
  { key: 'recent',     label: 'Most recent' },
  { key: 'name_asc',   label: 'A–Z' },
]

function loadPinned() {
  try {
    const raw = localStorage.getItem(PINNED_KEY)
    return raw ? new Set(JSON.parse(raw)) : new Set()
  } catch { return new Set() }
}

function savePinned(set) {
  try { localStorage.setItem(PINNED_KEY, JSON.stringify([...set])) } catch { /* ignore */ }
}

function loadSortMode() {
  try {
    const raw = localStorage.getItem(SORT_KEY)
    return SORT_OPTIONS.some(o => o.key === raw) ? raw : 'count_asc'
  } catch { return 'count_asc' }
}

function saveSortMode(mode) {
  try { localStorage.setItem(SORT_KEY, mode) } catch { /* ignore */ }
}

function comparatorFor(sortMode) {
  switch (sortMode) {
    case 'count_asc': return (a, b) => a.count - b.count
    case 'name_asc':  return (a, b) => a.commonName.localeCompare(b.commonName)
    case 'recent':    return (a, b) => new Date(b.lastSeen) - new Date(a.lastSeen)
    default:          return (a, b) => b.count - a.count // count_desc
  }
}

export default function SpeciesSummaryList({ minConf, species, datePreset, customFrom, customTo, moment }) {
  const [summary, setSummary]   = useState([])
  const [error, setError]       = useState(null)
  const [expanded, setExpanded] = useState(() => new Set())
  const [details, setDetails]   = useState({})       // commonName -> rows | 'loading' | 'error'
  const [pinned, setPinned]     = useState(loadPinned)
  const [sortMode, setSortMode] = useState(loadSortMode)

  const fetchSummary = useCallback(async () => {
    try {
      const params = buildDetectionParams({
        minConf, species, datePreset, customFrom, customTo, moment, limit: SUMMARY_LIMIT,
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
  }, [minConf, species, datePreset, customFrom, customTo, moment])

  useEffect(() => {
    fetchSummary()
    const t = setInterval(fetchSummary, POLL_INTERVAL_MS)
    return () => clearInterval(t)
  }, [fetchSummary])

  // Species no longer in the summary (e.g. filters changed) shouldn't keep
  // stale expanded/detail state around indefinitely, but it's harmless to
  // leave it — re-expanding re-fetches. Not worth pruning for v1. Pinned
  // selections, by contrast, are meant to persist across filter changes and
  // sessions (that's the point of saving them to localStorage), so a
  // species pinned today still shows pinned if it reappears after a filter
  // change or a future visit.

  function togglePin(commonName) {
    setPinned(prev => {
      const next = new Set(prev)
      if (next.has(commonName)) next.delete(commonName)
      else next.add(commonName)
      savePinned(next)
      return next
    })
  }

  function changeSortMode(mode) {
    setSortMode(mode)
    saveSortMode(mode)
  }

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
        minConf, species: commonName, datePreset, customFrom, customTo, moment, limit: DETAIL_LIMIT,
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
    toolbar: {
      display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
      padding: '8px 12px',
      borderBottom: '1px solid var(--border, #333)',
    },
    sortLabel: { fontSize: 11, color: 'var(--text-muted, #888)', marginRight: 4 },
    sortBtn: (active) => ({
      padding: '3px 9px', fontSize: 11, borderRadius: 12,
      border: `1px solid ${active ? 'var(--accent, #4da6ff)' : 'var(--border, #333)'}`,
      background: active ? 'var(--accent, #4da6ff)' : 'transparent',
      color: active ? '#fff' : 'var(--text-muted, #888)',
      cursor: 'pointer', whiteSpace: 'nowrap',
    }),
    sectionLabel: {
      fontSize: 11, color: 'var(--text-muted, #888)', fontWeight: 500,
      padding: '6px 12px 4px', textTransform: 'uppercase', letterSpacing: 0.5,
    },
    row: {
      display: 'flex', alignItems: 'center', gap: 12,
      padding: '8px 12px', cursor: 'pointer',
      borderBottom: '1px solid var(--border-faint, #2a2a2a)',
    },
    pinBox: { cursor: 'pointer', accentColor: 'var(--accent, #4da6ff)' },
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

  function renderRow(s) {
    const open = expanded.has(s.commonName)
    const detail = details[s.commonName]
    const isPinned = pinned.has(s.commonName)
    return (
      <div key={s.commonName}>
        <div style={sty.row} onClick={() => toggleExpand(s.commonName)}>
          <input
            type="checkbox" checked={isPinned} style={sty.pinBox}
            title={isPinned ? 'Unpin' : 'Pin to top'}
            onChange={() => togglePin(s.commonName)}
            onClick={e => e.stopPropagation()}
          />
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
  }

  const toolbar = (
    <div style={sty.toolbar}>
      <span style={sty.sortLabel}>Sort</span>
      {SORT_OPTIONS.map(o => (
        <button
          key={o.key}
          style={sty.sortBtn(sortMode === o.key)}
          onClick={() => changeSortMode(o.key)}
        >
          {o.label}
        </button>
      ))}
    </div>
  )

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
        {toolbar}
        <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted, #888)', fontSize: 13 }}>
          No detections match the current filters.
        </div>
      </div>
    )
  }

  const cmp = comparatorFor(sortMode)
  const pinnedRows = summary.filter(s => pinned.has(s.commonName)).sort(cmp)
  const otherRows = summary.filter(s => !pinned.has(s.commonName)).sort(cmp)

  return (
    <div style={sty.root}>
      {toolbar}
      {pinnedRows.length > 0 && (
        <>
          <div style={sty.sectionLabel}>Pinned</div>
          {pinnedRows.map(renderRow)}
        </>
      )}
      {pinnedRows.length > 0 && otherRows.length > 0 && (
        <div style={sty.sectionLabel}>All species</div>
      )}
      {otherRows.map(renderRow)}
    </div>
  )
}
