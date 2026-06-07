import { useState, useEffect } from 'react'

function relativeTime(isoString) {
  if (!isoString) return null
  const diff = Date.now() - new Date(isoString).getTime()
  if (diff < 60000) return `${Math.floor(diff / 1000)}s ago`
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`
  return `${Math.floor(diff / 3600000)}h ago`
}

function clockAccuracyLabel(node) {
  const us = node.clock?.accuracyUs
  // Firmware doesn't report a numeric accuracy yet (PPS discipline pending) —
  // show the source instead of fabricating a figure.
  if (us == null) return '—'
  if (us < 1000) return `±${us}µs`
  if (us < 1000000) return `±${(us/1000).toFixed(0)}ms`
  return `±${(us/1000000).toFixed(1)}s`
}

function clockAccuracyColor(node) {
  const us = node.clock?.accuracyUs
  if (us == null) return 'var(--text-muted)'
  if (us <= 100)   return 'var(--green)'
  if (us <= 5000)  return 'var(--yellow)'
  return 'var(--red)'
}

function bufferFraction(node) {
  const used = node.audio?.bufferUsedS
  const cap = node.audio?.bufferCapacityS
  if (used == null || !cap) return 0
  return used / cap
}

function bufferClass(fraction) {
  if (fraction >= 0.95) return 'full'
  if (fraction >= 0.75) return 'warn'
  return 'ok'
}

export default function NodeCard({ node, selected, onSelect }) {
  const [, setTick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setTick(n => n + 1), 5000)
    return () => clearInterval(t)
  }, [])

  const frac = bufferFraction(node)
  const borderColor = {
    online:   'var(--green)',
    degraded: 'var(--yellow)',
    offline:  'var(--red)',
  }[node.status]

  return (
    <div
      onClick={() => onSelect(node.id)}
      style={{
        padding: '10px 12px',
        borderLeft: `3px solid ${borderColor}`,
        background: selected ? 'var(--bg-card-hover)' : 'var(--bg-card)',
        borderRadius: '0 6px 6px 0',
        cursor: 'pointer',
        transition: 'background 0.15s',
        display: 'flex', flexDirection: 'column', gap: 7,
        outline: selected ? '1px solid var(--border)' : 'none',
      }}
      onMouseEnter={e => { if (!selected) e.currentTarget.style.background = 'var(--bg-card-hover)' }}
      onMouseLeave={e => { if (!selected) e.currentTarget.style.background = 'var(--bg-card)' }}
    >
      {/* Row 1: hostname + role + status dot */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
        <div className={`status-dot ${node.status}`} />
        <span style={{ fontWeight: 600, fontSize: 13, flex: 1 }}>{node.hostname}</span>
        <span className={`badge badge-${node.role.toLowerCase()}`}>{node.role}</span>
      </div>

      {/* Row 2: clock accuracy + RSSI */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>⏱</span>
          <span style={{ fontSize: 11, color: clockAccuracyColor(node) }}>
            {clockAccuracyLabel(node)}
          </span>
          {node.clock?.source === 'GPS_NMEA' && (
            <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>(NMEA)</span>
          )}
          {node.clock?.source === 'GPS_PPS' && (
            <span style={{ fontSize: 10, color: 'var(--green)' }}>(PPS)</span>
          )}
        </div>
        {node.espNow && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginLeft: 'auto' }}>
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>📶</span>
            <span style={{ fontSize: 11, color: node.espNow.rssi > -70 ? 'var(--green)' : node.espNow.rssi > -80 ? 'var(--yellow)' : 'var(--red)' }}>
              {node.espNow.rssi} dBm
            </span>
          </div>
        )}
        {node.gps && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginLeft: 'auto' }}>
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>📡</span>
            <span style={{ fontSize: 11, color: 'var(--green)' }}>
              {node.gps.satellites} sat
            </span>
          </div>
        )}
      </div>

      {/* Row 3: buffer fill bar */}
      {node.audio && (
        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
            <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>Buffer</span>
            <span style={{ fontSize: 10, color: 'var(--text-secondary)' }}>
              {node.audio.bufferUsedS != null ? `${node.audio.bufferUsedS.toFixed(0)}s` : '—'}
              {' / '}
              {node.audio.bufferCapacityS != null ? `${node.audio.bufferCapacityS.toFixed(0)}s` : '—'}
            </span>
          </div>
          <div className="fill-bar">
            <div
              className={`fill-bar-inner ${bufferClass(frac)}`}
              style={{ width: `${frac * 100}%` }}
            />
          </div>
        </div>
      )}

      {/* Row 4: flags / last trigger */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
        {node.flags?.includes('POSITION_UNKNOWN') && (
          <span style={{
            fontSize: 10, color: 'var(--yellow)',
            background: 'var(--yellow-dim)', padding: '1px 5px', borderRadius: 3,
          }}>POS UNKNOWN</span>
        )}
        {node.flags?.includes('CLOCK_UNSETTLED') && (
          <span style={{
            fontSize: 10, color: 'var(--orange)',
            background: 'rgba(219,109,40,0.15)', padding: '1px 5px', borderRadius: 3,
          }}>CLK UNSETTLED</span>
        )}
        {node.flags?.includes('CLOCK_INVALID') && (
          <span style={{
            fontSize: 10, color: 'var(--red)',
            background: 'rgba(219,68,55,0.15)', padding: '1px 5px', borderRadius: 3,
          }}>CLOCK INVALID</span>
        )}
        {node.flags?.includes('UNREACHABLE') && (
          <span style={{
            fontSize: 10, color: 'var(--red)',
            background: 'rgba(219,68,55,0.15)', padding: '1px 5px', borderRadius: 3,
          }}>UNREACHABLE</span>
        )}
        {node.flags?.includes('POSITION_DERIVED') && (
          <span
            title="Map position calculated from relative E/N/Alt offset, not surveyed/GPS-determined"
            style={{
              fontSize: 10, color: 'var(--blue)',
              background: 'var(--blue-dim)', padding: '1px 5px', borderRadius: 3,
            }}
          >POS DERIVED</span>
        )}
        {!node.flags?.length && node.audio?.lastTriggerAt && (
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            Triggered {relativeTime(node.audio.lastTriggerAt)}
          </span>
        )}
        {!node.flags?.length && !node.audio?.lastTriggerAt && (
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>No trigger data yet</span>
        )}
      </div>
    </div>
  )
}
