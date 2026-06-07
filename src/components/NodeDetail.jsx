import { useState, useEffect } from 'react'

function relativeTime(isoString) {
  if (!isoString) return '—'
  const diff = Date.now() - new Date(isoString).getTime()
  if (diff < 60000) return `${Math.floor(diff / 1000)}s ago`
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`
  return `${Math.floor(diff / 3600000)}h ago`
}

function fmt(val, suffix = '') {
  return val != null ? `${val}${suffix}` : '—'
}

function clockSourceLabel(source) {
  const map = {
    GPS_PPS:       'GPS PPS (hardware)',
    GPS_NMEA:      'GPS NMEA (stage 1)',
    ESPNOW_KALMAN: 'ESP-NOW + Kalman',
    NONE:          'None',
  }
  return map[source] ?? source
}

function clockAccuracyColor(us) {
  if (us == null) return ''
  if (us <= 100)   return 'good'
  if (us <= 5000)  return 'warn'
  return 'bad'
}

function fmtAccuracy(us) {
  if (us == null) return '—'
  if (us < 1000)  return `±${us} µs`
  if (us < 1000000) return `±${(us/1000).toFixed(0)} ms`
  return `±${(us/1000000).toFixed(1)} s`
}

function bufferFraction(node) {
  const used = node.audio?.bufferUsedS
  const cap = node.audio?.bufferCapacityS
  if (used == null || !cap) return 0
  return used / cap
}

function fmtNum(val, decimals = 2, suffix = '') {
  return val != null ? `${val.toFixed(decimals)}${suffix}` : '—'
}

function fmtFix(fix) {
  if (!fix) return '—'
  return `${fix.lat.toFixed(6)}, ${fix.lon.toFixed(6)} · ${fmtNum(fix.altM, 1, 'm')}`
}

function bufferClass(f) {
  if (f >= 0.95) return 'full'
  if (f >= 0.75) return 'warn'
  return 'ok'
}

export default function NodeDetail({ node, onClose }) {
  const [, setTick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setTick(n => n + 1), 1000)
    return () => clearInterval(t)
  }, [])

  const statusColor = { online: 'var(--green)', degraded: 'var(--yellow)', offline: 'var(--red)' }[node.status]
  const frac = bufferFraction(node)
  const relPos = node.positionRelative

  return (
    <div style={{
      width: 320, flexShrink: 0,
      background: 'var(--bg-panel)',
      borderLeft: '1px solid var(--border)',
      display: 'flex', flexDirection: 'column',
      overflow: 'hidden',
    }}>
      {/* Header */}
      <div style={{
        padding: '10px 14px',
        borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', gap: 8,
      }}>
        <div className={`status-dot ${node.status}`} />
        <div style={{ flex: 1 }}>
          <div style={{ fontWeight: 700, fontSize: 14 }}>{node.hostname}</div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 1 }}>
            {node.ipAddress ?? '—'} · fw {node.firmwareVersion ?? '—'}
          </div>
        </div>
        <span className={`badge badge-${node.role.toLowerCase()}`}>{node.role}</span>
        <button
          onClick={onClose}
          style={{
            background: 'none', border: 'none', color: 'var(--text-muted)',
            cursor: 'pointer', fontSize: 16, lineHeight: 1, padding: '0 0 0 4px',
          }}
          title="Close"
        >×</button>
      </div>

      {/* Flags */}
      {node.flags?.length > 0 && (
        <div style={{
          padding: '8px 14px',
          background: 'var(--yellow-dim)',
          borderBottom: '1px solid rgba(210,153,34,0.3)',
          display: 'flex', flexDirection: 'column', gap: 4,
        }}>
          {node.flags.map(flag => (
            <div key={flag} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{ color: 'var(--yellow)', fontSize: 12 }}>⚠</span>
              <span style={{ color: 'var(--yellow)', fontSize: 12 }}>
                {flag === 'POSITION_UNKNOWN' && 'Position unknown — TOF calibration required'}
                {flag === 'CLOCK_UNSETTLED'  && 'Clock not yet settled — Kalman still converging'}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Scrollable content */}
      <div style={{ flex: 1, overflowY: 'auto', padding: 14, display: 'flex', flexDirection: 'column', gap: 18 }}>

        {/* Position */}
        <section>
          <div className="section-label">Position</div>
          {relPos ? (
            <>
              <div className="kv">
                <span className="kv-key">E / N / Alt</span>
                <span className="kv-val" style={{ fontFamily: 'monospace', fontSize: 11 }}>
                  {relPos.eM.toFixed(2)}m · {relPos.nM.toFixed(2)}m · {relPos.altM > 0 ? '+' : ''}{relPos.altM.toFixed(2)}m
                </span>
              </div>
              <div className="kv">
                <span className="kv-key">Lat / Lon / Alt</span>
                <span className="kv-val" style={{ fontFamily: 'monospace', fontSize: 11 }}>
                  {fmtFix(node.gps?.origin ?? node.gps?.centroid ?? node.latLon)}
                </span>
              </div>
              <div className="kv">
                <span className="kv-key">Status</span>
                <span className={`kv-val ${node.gps?.origin ? 'good' : 'warn'}`}>
                  {node.gps?.origin ? 'Surveyed' : 'Estimated'}
                </span>
              </div>
              {node.gps?.origin ? (
                <div style={{
                  marginTop: 4, padding: '5px 8px',
                  background: 'var(--green-dim)', borderRadius: 4,
                  fontSize: 11, color: 'var(--green)',
                }}>
                  Lat/lon/alt is the surveyed position configured on this node
                </div>
              ) : (
                /* No surveyed origin reported (non-primary node, or not yet
                   configured) — fall back to the GPS centroid (long-run
                   average), the most stable absolute estimate available. */
                <div style={{
                  marginTop: 4, padding: '5px 8px',
                  background: 'var(--blue-dim)', borderRadius: 4,
                  fontSize: 11, color: 'var(--blue)',
                }}>
                  Lat/lon/alt is the GPS centroid average — no surveyed position is configured on this node
                </div>
              )}
            </>
          ) : (
            <div className="kv">
              <span className="kv-key">Status</span>
              <span className="kv-val warn">Unknown — pending TOF calibration</span>
            </div>
          )}
        </section>

        {/* Clock */}
        <section>
          <div className="section-label">Clock Sync</div>
          <div className="kv">
            <span className="kv-key">Source</span>
            <span className="kv-val" style={{ fontSize: 11 }}>{clockSourceLabel(node.clock?.source)}</span>
          </div>
          <div className="kv">
            <span className="kv-key">Accuracy</span>
            <span className={`kv-val ${clockAccuracyColor(node.clock?.accuracyUs)}`}>
              {fmtAccuracy(node.clock?.accuracyUs)}
            </span>
          </div>
          {node.clock?.offsetUs != null && (
            <div className="kv">
              <span className="kv-key">Offset vs primary</span>
              <span className="kv-val" style={{ fontFamily: 'monospace' }}>
                {node.clock.offsetUs > 0 ? '+' : ''}{node.clock.offsetUs} µs
              </span>
            </div>
          )}
          {node.clock?.valid != null && (
            <div className="kv">
              <span className="kv-key">Valid</span>
              <span className={`kv-val ${node.clock.valid ? 'good' : 'bad'}`}>
                {node.clock.valid ? 'Yes' : 'No — not yet synchronised'}
              </span>
            </div>
          )}
          {node.clock?.kalmanSettled != null && (
            <div className="kv">
              <span className="kv-key">Kalman</span>
              <span className={`kv-val ${node.clock.kalmanSettled ? 'good' : 'warn'}`}>
                {node.clock.kalmanSettled ? 'Settled' : 'Converging…'}
              </span>
            </div>
          )}
          {node.clock?.source === 'GPS_NMEA' && (
            <div style={{
              marginTop: 6, padding: '5px 8px',
              background: 'var(--blue-dim)', borderRadius: 4,
              fontSize: 11, color: 'var(--blue)',
            }}>
              PPS hardware pending — accuracy limited to NMEA (~50ms)
            </div>
          )}
        </section>

        {/* GPS — primary node only */}
        {node.gps && (
          <section>
            <div className="section-label">GPS</div>
            <div className="kv">
              <span className="kv-key">Lock</span>
              <span className={`kv-val ${node.gps.locked ? 'good' : 'bad'}`}>
                {node.gps.locked ? `Yes — ${node.gps.satellites} satellites` : 'No lock'}
              </span>
            </div>
            {node.gps.origin && (
              <div className="kv">
                <span className="kv-key">Surveyed origin</span>
                <span className="kv-val good" style={{ fontFamily: 'monospace', fontSize: 11 }}>
                  {fmtFix(node.gps.origin)}
                </span>
              </div>
            )}
            <div className="kv">
              <span className="kv-key">Live fix</span>
              <span className="kv-val" style={{ fontFamily: 'monospace', fontSize: 11 }}>
                {fmtFix(node.gps.live)}
              </span>
            </div>
            <div className="kv">
              <span className="kv-key">EMA fix</span>
              <span className="kv-val" style={{ fontFamily: 'monospace', fontSize: 11 }}>
                {fmtFix(node.gps.ema)}
              </span>
            </div>
            <div className="kv">
              <span className="kv-key">Centroid fix</span>
              <span className="kv-val good" style={{ fontFamily: 'monospace', fontSize: 11 }}>
                {fmtFix(node.gps.centroid)}
              </span>
            </div>
            <div className="kv">
              <span className="kv-key">Centroid N</span>
              <span className="kv-val">
                {node.gps.centroidN != null ? `${node.gps.centroidN.toLocaleString()} samples` : '—'}
              </span>
            </div>
            <div className="kv">
              <span className="kv-key">Centroid σ</span>
              <span className={`kv-val ${node.gps.centroidStddevM == null ? '' : node.gps.centroidStddevM < 2 ? 'good' : node.gps.centroidStddevM < 5 ? 'warn' : 'bad'}`}>
                {fmtNum(node.gps.centroidStddevM, 2, ' m')}
              </span>
            </div>
            <div className="kv">
              <span className="kv-key">Divergence</span>
              <span className={`kv-val ${node.gps.divergenceM == null ? '' : node.gps.divergenceM < 2 ? 'good' : node.gps.divergenceM < 5 ? 'warn' : 'bad'}`}>
                {fmtNum(node.gps.divergenceM, 2, ' m')}
              </span>
            </div>
            <div className="kv">
              <span className="kv-key">N / E / Alt Δ</span>
              <span className="kv-val" style={{ fontSize: 11, fontFamily: 'monospace' }}>
                {fmtNum(node.gps.divergenceN)} · {fmtNum(node.gps.divergenceE)} · {fmtNum(node.gps.divergenceAlt)} m
              </span>
            </div>
          </section>
        )}

        {/* ESP-NOW — leaf nodes */}
        {node.espNow && (
          <section>
            <div className="section-label">ESP-NOW</div>
            <div className="kv">
              <span className="kv-key">RSSI</span>
              <span className={`kv-val ${node.espNow.rssi > -70 ? 'good' : node.espNow.rssi > -80 ? 'warn' : 'bad'}`}>
                {node.espNow.rssi} dBm
              </span>
            </div>
            <div className="kv">
              <span className="kv-key">Hop count</span>
              <span className="kv-val">{node.espNow.hopCount}</span>
            </div>
            <div className="kv">
              <span className="kv-key">Last heartbeat</span>
              <span className="kv-val">{relativeTime(node.espNow.lastHeartbeatAt)}</span>
            </div>
          </section>
        )}

        {/* Audio buffer */}
        {node.audio && (
          <section>
            <div className="section-label">Audio Buffer</div>
            <div style={{ marginBottom: 8 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 5 }}>
                <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>Fill</span>
                <span style={{ fontSize: 12, color: 'var(--text-primary)', fontVariantNumeric: 'tabular-nums' }}>
                  {fmtNum(node.audio.bufferUsedS, 0, 's')} / {fmtNum(node.audio.bufferCapacityS, 0, 's')} ({Math.round(frac * 100)}%)
                </span>
              </div>
              <div className="fill-bar" style={{ height: 6 }}>
                <div className={`fill-bar-inner ${bufferClass(frac)}`} style={{ width: `${frac * 100}%` }} />
              </div>
            </div>
            <div className="kv">
              <span className="kv-key">Format</span>
              <span className="kv-val">
                {node.audio.sampleRateHz != null ? `${(node.audio.sampleRateHz / 1000).toFixed(0)} kHz` : '—'}
                {' / '}
                {node.audio.bitDepth != null ? `${node.audio.bitDepth}-bit` : '— -bit (not reported)'} mono
              </span>
            </div>
            {node.audio.running != null && (
              <div className="kv">
                <span className="kv-key">Capture</span>
                <span className={`kv-val ${node.audio.running ? 'good' : 'bad'}`}>
                  {node.audio.running ? 'Running' : 'Stopped'}
                </span>
              </div>
            )}
            <div className="kv">
              <span className="kv-key">Last trigger</span>
              <span className="kv-val">
                {node.audio.lastTriggerAt ? relativeTime(node.audio.lastTriggerAt) : '— (not yet reported)'}
              </span>
            </div>
          </section>
        )}

        {/* Last seen */}
        <div className="kv" style={{ borderTop: '1px solid var(--border-muted)', paddingTop: 8 }}>
          <span className="kv-key">Last seen</span>
          <span className="kv-val">{relativeTime(node.lastSeenAt)}</span>
        </div>
      </div>

      {/* Actions footer */}
      <div style={{
        padding: '10px 14px',
        borderTop: '1px solid var(--border)',
        display: 'flex', flexDirection: 'column', gap: 7,
      }}>
        <div style={{ display: 'flex', gap: 7 }}>
          <button
            className="btn btn-primary"
            style={{ flex: 1 }}
            disabled={node.status === 'offline'}
            title={node.status === 'offline' ? 'Node offline' : 'Request audio sample from this node'}
          >
            Request Sample
          </button>
          <button
            className="btn"
            disabled={node.status === 'offline'}
            title="View buffer inventory"
          >
            Inventory
          </button>
        </div>
        {!node.positionKnown && (
          <button className="btn" style={{ width: '100%', borderColor: 'var(--yellow)', color: 'var(--yellow)' }}>
            Begin TOF Calibration
          </button>
        )}
        <button className="btn" style={{ width: '100%' }} disabled title="Not yet implemented">
          Configure
        </button>
      </div>
    </div>
  )
}
