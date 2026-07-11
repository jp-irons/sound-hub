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

function bufferFraction(node) {
  const used = node.audio?.bufferUsedS
  const cap = node.audio?.bufferCapacityS
  if (used == null || !cap) return 0
  return used / cap
}

function bufferClass(fraction) {
  if (fraction >= 0.90) return 'good'
  if (fraction >= 0.25) return 'warn'
  return 'low'
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
        transition: 'background 0.15s, box-shadow 0.15s',
        display: 'flex', flexDirection: 'column', gap: 7,
        // Echoes the white-ring highlight the selected node gets on the map —
        // a thin neutral outline was too subtle to notice against the dark panel.
        boxShadow: selected ? '0 0 0 1.5px var(--blue), 0 0 8px rgba(56,139,253,0.35)' : 'none',
      }}
      onMouseEnter={e => { if (!selected) e.currentTarget.style.background = 'var(--bg-card-hover)' }}
      onMouseLeave={e => { if (!selected) e.currentTarget.style.background = 'var(--bg-card)' }}
    >
      {/* Row 1: hostname + anchor indicators + status dot */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
        <div className={`status-dot ${node.status}`} />
        {node.ipAddress ? (
          <a
            href={`https://${node.ipAddress}`}
            target={node.id}
            onClick={e => e.stopPropagation()}
            style={{ fontWeight: 600, fontSize: 13, flex: 1, color: 'inherit' }}
          >
            {node.hostname}
          </a>
        ) : (
          <span style={{ fontWeight: 600, fontSize: 13, flex: 1 }}>{node.hostname}</span>
        )}
        {node.role === 'BROKER' && (
          <span title="Broker — relays ESP-NOW traffic to/from WiFi" style={{
            fontSize: 10, color: 'var(--text-primary)',
            background: 'var(--border)', padding: '1px 5px', borderRadius: 3, fontWeight: 700,
          }}>BROKER</span>
        )}
      </div>

      {/* Row 2: clock accuracy + RSSI */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>⏱</span>
          {(() => {
            const s = node.clock?.source
            const labelColor = {
              GPS_PPS:              'var(--green)',
              GPS_NMEA:             'var(--text-muted)',
              NETWORK_GPS_PPS:      'var(--blue)',
              NETWORK_GPS_NMEA:     'var(--text-muted)',
              NETWORK_FREE_RUNNING: 'var(--yellow)',
              FREE_RUNNING:         'var(--yellow)',
            }[s]
            const label = {
              GPS_PPS:              'PPS',
              GPS_NMEA:             'NMEA',
              NETWORK_GPS_PPS:      'Net PPS',
              NETWORK_GPS_NMEA:     'Net NMEA',
              NETWORK_FREE_RUNNING: 'Net',
              FREE_RUNNING:         'free',
            }[s]
            if (!label) return null
            const us = node.clock?.accuracyUs
            return (
              <span style={{ fontSize: 11, color: labelColor }}>
                {label}{us != null && ` (${clockAccuracyLabel(node)})`}
              </span>
            )
          })()}
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

      {/* Row 3: buffer fill bar — not shown for brokers, no mic assumed */}
      {node.audio && node.role !== 'BROKER' && (
        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
            <span style={{ fontSize: 10, color: 'var(--text-secondary)' }}>Buffer</span>
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
        {node.flags?.includes('CLOCK_NO_UTC') && (
          <span style={{
            fontSize: 10, color: 'var(--yellow)',
            background: 'var(--yellow-dim)', padding: '1px 5px', borderRadius: 3,
          }}>NO UTC</span>
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
        {node.flags?.includes('AUDIO_STOPPED') && (
          <span
            title="SD card absent or failed — audio capture not running"
            style={{
              fontSize: 10, color: 'var(--red)',
              background: 'rgba(219,68,55,0.15)', padding: '1px 5px', borderRadius: 3,
            }}
          >NO AUDIO</span>
        )}
        {/* Surveyed (operator-confirmed, fixed anchor) vs Estimated
            (provisional, refined by ongoing calibration) — the
            operator-set provenance flag for this node's position.
            More actionable at a glance than POSITION_DERIVED, which is
            really about lat/lon-projection mechanics, not trust level. */}
        {node.positionKnown && (
          node.positionStatus === 'surveyed' ? (
            <span
              title="Position is operator-confirmed ground truth — treated as a fixed anchor"
              style={{
                fontSize: 10, color: 'var(--green)',
                background: 'var(--green-dim)', padding: '1px 5px', borderRadius: 3,
              }}
            >SURVEYED</span>
          ) : (
            <span
              title="Position is provisional — subject to refinement by ongoing calibration"
              style={{
                fontSize: 10, color: 'var(--blue)',
                background: 'var(--blue-dim)', padding: '1px 5px', borderRadius: 3,
              }}
            >ESTIMATED</span>
          )
        )}
        {node.role !== 'BROKER' && !node.flags?.length && node.audio?.lastTriggerAt && (
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            Triggered {relativeTime(node.audio.lastTriggerAt)}
          </span>
        )}
        {node.role !== 'BROKER' && !node.flags?.length && !node.audio?.lastTriggerAt && (
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>No trigger data yet</span>
        )}
      </div>
    </div>
  )
}
