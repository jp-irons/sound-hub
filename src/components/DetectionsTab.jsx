import { useState, useEffect, useCallback } from 'react'

const API_BASE = '/api'
const POLL_INTERVAL_MS = 5000

const CONF_HIGH  = 0.8
const CONF_MED   = 0.5

function confidenceColour(conf) {
  if (conf >= CONF_HIGH) return 'var(--green,  #4caf50)'
  if (conf >= CONF_MED)  return 'var(--yellow, #ffc107)'
  return 'var(--red, #f44336)'
}

function ConfBar({ value }) {
  const pct = Math.round(value * 100)
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <div style={{
        width: 80, height: 8, background: 'var(--surface2, #2a2a2a)',
        borderRadius: 4, overflow: 'hidden',
      }}>
        <div style={{
          width: `${pct}%`, height: '100%',
          background: confidenceColour(value),
          borderRadius: 4,
          transition: 'width 0.2s',
        }} />
      </div>
      <span style={{ fontSize: 11, color: 'var(--text-muted, #888)', minWidth: 32 }}>
        {pct}%
      </span>
    </div>
  )
}

function formatTime(iso) {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch { return iso }
}

// Local-day boundaries (browser timezone) for the date-range presets.
function startOfDay(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x }
function endOfDay(d)   { const x = new Date(d); x.setHours(23, 59, 59, 999); return x }

const DATE_PRESETS = [
  { key: 'all',       label: 'All' },
  { key: 'today',     label: 'Today' },
  { key: 'yesterday', label: 'Yesterday' },
  { key: 'last7',     label: 'Last 7 days' },
  { key: 'custom',    label: 'Custom' },
]

// Resolve a preset key (or explicit custom from/to date strings) to a
// {from, to} Date range. Returns null for 'all' (no bound) or an incomplete
// custom range.
function resolveRange(preset, customFrom, customTo) {
  const now = new Date()
  switch (preset) {
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

export default function DetectionsTab() {
  const [detections, setDetections]   = useState([])
  const [minConf, setMinConf]         = useState(0.0)
  const [species, setSpecies]         = useState('')
  const [datePreset, setDatePreset]   = useState('all')
  const [customFrom, setCustomFrom]   = useState('') // yyyy-mm-dd
  const [customTo, setCustomTo]       = useState('') // yyyy-mm-dd

  const fetchDetections = useCallback(async () => {
    try {
      const params = new URLSearchParams({ limit: 200, min_conf: minConf })
      if (species.trim()) params.set('species', species.trim())
      const range = resolveRange(datePreset, customFrom, customTo)
      if (range?.from) params.set('from', range.from.toISOString())
      if (range?.to) params.set('to', range.to.toISOString())
      const res = await fetch(`${API_BASE}/detections?${params}`)
      if (!res.ok) throw new Error(`${res.status}`)
      setDetections(await res.json())
    } catch { /* backend may not be up yet */ }
  }, [minConf, species, datePreset, customFrom, customTo])

  useEffect(() => {
    fetchDetections()
    const t = setInterval(fetchDetections, POLL_INTERVAL_MS)
    return () => clearInterval(t)
  }, [fetchDetections])

  const sty = {
    root: {
      display: 'flex', flexDirection: 'column', height: '100%',
      padding: '16px 20px', gap: 16, boxSizing: 'border-box',
      overflow: 'hidden',
    },
    row: { display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' },
    input: {
      background: 'var(--surface2, #2a2a2a)',
      border: '1px solid var(--border, #333)',
      borderRadius: 4, padding: '4px 8px',
      color: 'var(--text, #eee)', fontSize: 12,
    },
    presetBtn: (active) => ({
      padding: '4px 10px', fontSize: 12, borderRadius: 14,
      border: `1px solid ${active ? 'var(--accent, #4da6ff)' : 'var(--border, #333)'}`,
      background: active ? 'var(--accent, #4da6ff)' : 'transparent',
      color: active ? '#fff' : 'var(--text-muted, #888)',
      cursor: 'pointer', whiteSpace: 'nowrap',
    }),
    table: { width: '100%', borderCollapse: 'collapse', fontSize: 12 },
    th: {
      textAlign: 'left', padding: '6px 10px',
      borderBottom: '1px solid var(--border, #333)',
      color: 'var(--text-muted, #888)', fontWeight: 500,
      position: 'sticky', top: 0,
      background: 'var(--surface1, #1e1e1e)',
    },
    td: {
      padding: '6px 10px',
      borderBottom: '1px solid var(--border-faint, #2a2a2a)',
      color: 'var(--text, #eee)',
    },
  }

  return (
    <div style={sty.root}>
      {/* ── Date range ── */}
      <div style={{ ...sty.row, gap: 8 }}>
        {DATE_PRESETS.map(p => (
          <button
            key={p.key}
            style={sty.presetBtn(datePreset === p.key)}
            onClick={() => setDatePreset(p.key)}
          >
            {p.label}
          </button>
        ))}
        {datePreset === 'custom' && (
          <>
            <input
              type="date" value={customFrom}
              onChange={e => setCustomFrom(e.target.value)}
              style={sty.input}
            />
            <span style={{ color: 'var(--text-muted, #888)', fontSize: 12 }}>to</span>
            <input
              type="date" value={customTo}
              onChange={e => setCustomTo(e.target.value)}
              style={sty.input}
            />
          </>
        )}
      </div>

      {/* ── Filter bar ── */}
      <div style={{ ...sty.row, gap: 12 }}>
        <label style={{ fontSize: 12, color: 'var(--text-muted, #888)', display: 'flex', alignItems: 'center', gap: 6 }}>
          Species filter
          <input
            type="text" placeholder="e.g. Kookaburra"
            value={species}
            onChange={e => setSpecies(e.target.value)}
            style={{ ...sty.input, width: 160 }}
          />
        </label>
        <label style={{ fontSize: 12, color: 'var(--text-muted, #888)', display: 'flex', alignItems: 'center', gap: 6 }}>
          Min confidence
          <input
            type="number" min="0" max="1" step="0.05"
            value={minConf}
            onChange={e => setMinConf(parseFloat(e.target.value))}
            style={{ ...sty.input, width: 60 }}
          />
        </label>
        <span style={{ fontSize: 11, color: 'var(--text-muted, #888)' }}>
          {detections.length} record{detections.length !== 1 ? 's' : ''}
        </span>
      </div>

      {/* ── Table ── */}
      <div style={{ flex: 1, overflow: 'auto', background: 'var(--surface1, #1e1e1e)', borderRadius: 8, border: '1px solid var(--border, #333)' }}>
        {detections.length === 0
          ? (
            <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted, #888)', fontSize: 13 }}>
              No detections yet.
            </div>
          )
          : (
            <table style={sty.table}>
              <thead>
                <tr>
                  <th style={sty.th}>Time</th>
                  <th style={sty.th}>Common name</th>
                  <th style={sty.th}>Scientific name</th>
                  <th style={sty.th}>Confidence</th>
                  <th style={sty.th}>Source</th>
                  <th style={sty.th}>Offset</th>
                </tr>
              </thead>
              <tbody>
                {detections.map(d => (
                  <tr key={d.id} style={{ cursor: 'default' }}>
                    <td style={sty.td}>{formatTime(d.analyzedAt)}</td>
                    <td style={{ ...sty.td, fontWeight: 500 }}>{d.commonName}</td>
                    <td style={{ ...sty.td, fontStyle: 'italic', color: 'var(--text-muted, #888)' }}>{d.scientificName}</td>
                    <td style={sty.td}><ConfBar value={d.confidence} /></td>
                    <td style={{ ...sty.td, color: 'var(--text-muted, #888)' }}>{d.source ?? '—'}</td>
                    <td style={{ ...sty.td, color: 'var(--text-muted, #888)' }}>
                      {d.startSec != null ? `${d.startSec}–${d.endSec}s` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        }
      </div>
    </div>
  )
}
