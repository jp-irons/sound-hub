import { useState } from 'react'
import { DATE_PRESETS, MOMENT_OPTIONS, isQuickMoment } from '../utils/detectionFilters.js'
import SpeciesSummaryList from './SpeciesSummaryList.jsx'

export default function DetectionsTab() {
  const [minConf, setMinConf]         = useState(0.0)
  const [species, setSpecies]         = useState('')
  const [datePreset, setDatePreset]   = useState('today')
  const [customFrom, setCustomFrom]   = useState('') // yyyy-mm-dd
  const [customTo, setCustomTo]       = useState('') // yyyy-mm-dd
  const [moment, setMoment]           = useState('') // '' | last10min | last1hour | dawn | daytime | dusk | nighttime

  // Quick recency windows (last10min/last1hour) and calendar date presets
  // both ultimately drive the same from/to range, so only one should be
  // "active" at a time — picking one clears the other rather than silently
  // overriding it underneath.
  function chooseDatePreset(key) {
    setDatePreset(key)
    if (isQuickMoment(moment)) setMoment('')
  }

  function chooseMoment(key) {
    setMoment(key)
  }

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
  }

  return (
    <div style={sty.root}>
      {/* ── Date range ── */}
      <div style={{ ...sty.row, gap: 8 }}>
        {DATE_PRESETS.map(p => (
          <button
            key={p.key}
            style={sty.presetBtn(datePreset === p.key)}
            onClick={() => chooseDatePreset(p.key)}
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

      {/* ── Moment: sun-relative time-of-day + quick recency windows, one
          active at a time (anchored to array_origin for the sun buckets) ── */}
      <div style={{ ...sty.row, gap: 8 }}>
        {MOMENT_OPTIONS.map(o => (
          <button
            key={o.key || 'all-day'}
            style={sty.presetBtn(moment === o.key)}
            onClick={() => chooseMoment(o.key)}
          >
            {o.label}
          </button>
        ))}
      </div>

      {/* ── Filter bar ── */}
      <div style={{ ...sty.row, gap: 12 }}>
        <label style={{ fontSize: 12, color: 'var(--text-muted, #888)', display: 'flex', alignItems: 'center', gap: 6 }}>
          Species search
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
            onChange={e => {
              const raw = e.target.value
              if (raw === '') { setMinConf(0); return }
              const val = parseFloat(raw)
              if (Number.isNaN(val)) return
              setMinConf(Math.min(1, Math.max(0, val)))
            }}
            style={{ ...sty.input, width: 60 }}
          />
        </label>
      </div>

      {/* ── Species list ── */}
      <SpeciesSummaryList
        minConf={minConf}
        species={species}
        datePreset={datePreset}
        customFrom={customFrom}
        customTo={customTo}
        moment={moment}
      />
    </div>
  )
}
