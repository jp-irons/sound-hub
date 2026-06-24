// Shared presentational pieces for detection rows — used by DetectionsTab.jsx
// and SpeciesSummaryList.jsx so confidence bars / timestamps render
// identically wherever a detection row shows up.

const CONF_HIGH = 0.8
const CONF_MED  = 0.5

export function confidenceColour(conf) {
  if (conf >= CONF_HIGH) return 'var(--green,  #4caf50)'
  if (conf >= CONF_MED)  return 'var(--yellow, #ffc107)'
  return 'var(--red, #f44336)'
}

export function ConfBar({ value }) {
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

export function formatTime(iso) {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch { return iso }
}

export function formatDateTime(iso) {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return d.toLocaleString([], {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  } catch { return iso }
}
