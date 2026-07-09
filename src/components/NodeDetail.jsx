import { useState, useEffect } from 'react'
import NodeConfigModal from './NodeConfigModal.jsx'
import NodePositionModal from './NodePositionModal.jsx'

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
    GPS_PPS:              'GPS PPS (hardware)',
    GPS_NMEA:             'GPS NMEA (stage 1)',
    NETWORK_GPS_PPS:      'Network — GPS PPS',
    NETWORK_GPS_NMEA:     'Network — GPS NMEA',
    NETWORK_FREE_RUNNING: 'Network — free running',
    FREE_RUNNING:         'Free running',
    NONE:                 'None',
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
  if (f >= 0.90) return 'good'
  if (f >= 0.25) return 'warn'
  return 'low'
}

export default function NodeDetail({ node, onClose, onApprove, onReject, onRemove, onConfigure, onSetPosition, isAdmin = false }) {
  const [, setTick] = useState(0)
  const [configOpen, setConfigOpen] = useState(false)
  const [positionOpen, setPositionOpen] = useState(false)
  useEffect(() => {
    const t = setInterval(() => setTick(n => n + 1), 1000)
    return () => clearInterval(t)
  }, [])

  const statusColor = { online: 'var(--green)', degraded: 'var(--yellow)', offline: 'var(--red)' }[node.status]
  const frac = bufferFraction(node)
  const relPos = node.positionRelative

  return (
    <div style={{
      flex: 1,
      background: 'var(--bg-panel)',
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
          <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 1 }}>
            {node.ipAddress ?? '—'} · fw {node.firmwareVersion ?? '—'}
          </div>
        </div>
        {node.role === 'BROKER' && (
          <span title="Broker — relays ESP-NOW traffic to/from WiFi" style={{
            fontSize: 10, color: 'var(--text-primary)',
            background: 'var(--border)', padding: '1px 6px', borderRadius: 3, fontWeight: 700,
            letterSpacing: '0.04em',
          }}>BROKER</span>
        )}
        <button
          onClick={onClose}
          style={{
            background: 'none', border: 'none', color: 'var(--text-muted)',
            cursor: 'pointer', fontSize: 16, lineHeight: 1, padding: '0 0 0 4px',
          }}
          title="Close"
        >×</button>
      </div>

      {/* Flags banner — POSITION_DERIVED is deliberately excluded here:
          it's already explained inline in the Position section below
          (right next to the lat/lon it qualifies), so repeating it up
          here is redundant noise rather than useful warning. */}
      {node.flags?.filter(f => f !== 'POSITION_DERIVED').length > 0 && (
        <div style={{
          padding: '8px 14px',
          background: 'var(--yellow-dim)',
          borderBottom: '1px solid rgba(210,153,34,0.3)',
          display: 'flex', flexDirection: 'column', gap: 4,
        }}>
          {node.flags.filter(flag => flag !== 'POSITION_DERIVED').map(flag => (
            <div key={flag} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{ color: 'var(--yellow)', fontSize: 12 }}>⚠</span>
              <span style={{ color: 'var(--yellow)', fontSize: 12 }}>
                {flag === 'POSITION_UNKNOWN' && 'Position unknown — set via Set Position'}
                {flag === 'CLOCK_UNSETTLED'  && 'Clock not yet settled — Kalman still converging'}
                {flag === 'CLOCK_NO_UTC'     && 'No UTC reference — TDOA relative only'}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Scrollable content */}
      <div style={{ flex: 1, overflowY: 'auto', padding: 14, display: 'flex', flexDirection: 'column', gap: 18 }}>

        {/* Position — not meaningful for a broker: it's not a sensing array
            member, so it may have no stored position at all (dedicated
            relay-only hardware) and TDOA geometry doesn't apply to it. */}
        {node.role !== 'BROKER' && (
        <section>
          <div className="section-label">Position</div>
          {relPos ? (
            <>
              <div className="kv">
                <span className="kv-key">N / E / Alt</span>
                <span className="kv-val" style={{ fontFamily: 'monospace', fontSize: 11 }}>
                  {relPos.nM.toFixed(2)}m · {relPos.eM.toFixed(2)}m · {relPos.altM > 0 ? '+' : ''}{relPos.altM.toFixed(2)}m
                </span>
              </div>
              {/* Lat/Lon — projected from hub array_origin + stored E/N offset,
                  or hub-side GPS EMA fallback when no array origin is set yet. */}
              {node.latLon && (
                <>
                  <div className="kv">
                    <span className="kv-key">Lat / Lon</span>
                    <span className="kv-val" style={{ fontFamily: 'monospace', fontSize: 11 }}>
                      {node.latLon.lat.toFixed(6)}, {node.latLon.lon.toFixed(6)}
                    </span>
                  </div>
                  {node.flags?.includes('POSITION_DERIVED') ? (
                    <div style={{
                      marginTop: 4, padding: '5px 8px',
                      background: 'var(--blue-dim)', borderRadius: 4,
                      fontSize: 11, color: 'var(--blue)',
                    }}>
                      Projected from hub array origin via N/E/Alt offset
                    </div>
                  ) : (
                    <div style={{
                      marginTop: 4, padding: '5px 8px',
                      background: 'var(--blue-dim)', borderRadius: 4,
                      fontSize: 11, color: 'var(--blue)',
                    }}>
                      GPS EMA estimate — array origin not yet configured
                    </div>
                  )}
                </>
              )}
              {/* This reflects the operator-set provenance flag
                  (`positionStatus`): "Surveyed" means the operator has
                  confirmed this E/N/Alt as ground truth — a fixed anchor
                  the solver/calibration won't auto-correct. "Estimated"
                  means it's still provisional and subject to refinement.
                  This is independent of whether the position is *known*
                  (handled by the relPos branch here) — a known position
                  can still be only an estimate. */}
              <div className="kv">
                <span className="kv-key">Status</span>
                {node.positionStatus === 'surveyed' ? (
                  <span className="kv-val good">Surveyed</span>
                ) : (
                  <span className="kv-val" style={{ color: 'var(--blue)' }}>Estimated</span>
                )}
              </div>
              {node.surveyDisagreementM != null && (
                <div className="kv">
                  <span className="kv-key">Survey Δ</span>
                  <span className={`kv-val ${node.surveyDisagreementM < 2 ? 'good' : node.surveyDisagreementM < 5 ? 'warn' : 'bad'}`}>
                    {node.surveyDisagreementM.toFixed(2)} m from live GPS EMA
                  </span>
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
        )}

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
          {node.clock?.syncAgeMs != null && (
            <div className="kv">
              <span className="kv-key">Last sync</span>
              <span className="kv-val">
                {node.clock.syncAgeMs < 1000
                  ? `${node.clock.syncAgeMs} ms ago`
                  : `${(node.clock.syncAgeMs / 1000).toFixed(1)} s ago`}
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
          {(node.clock?.source === 'GPS_NMEA' || node.clock?.source === 'NETWORK_GPS_NMEA') && (
            <div style={{
              marginTop: 6, padding: '5px 8px',
              background: 'var(--blue-dim)', borderRadius: 4,
              fontSize: 11, color: 'var(--blue)',
            }}>
              PPS hardware pending — accuracy limited to NMEA (~50ms)
            </div>
          )}
        </section>

        {/* GPS — not shown for brokers; survey/divergence is a sensing-array
            concern and doesn't apply to a relay-only node. */}
        {node.gps && node.role !== 'BROKER' && (
          <section>
            <div className="section-label">GPS</div>
            <div className="kv">
              <span className="kv-key">Lock</span>
              <span className={`kv-val ${node.gps.locked ? 'good' : 'bad'}`}>
                {node.gps.locked ? `Yes — ${node.gps.satellites} satellites` : 'No lock'}
              </span>
            </div>
            {false && /* node.gps.origin removed — origin is now a hub-level config */ (
              <div className="kv">
                <span className="kv-key">Surveyed origin</span>
                <span className="kv-val good" style={{ fontFamily: 'monospace', fontSize: 11 }}>
                  —
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
              <span className="kv-val good" style={{ fontFamily: 'monospace', fontSize: 11 }}>
                {fmtFix(node.gps.ema)}
              </span>
            </div>
            <div className="kv">
              <span className="kv-key">EMA samples</span>
              <span className="kv-val">
                {node.gps.emaN != null ? `${node.gps.emaN.toLocaleString()} samples` : '—'}
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

      {/* Actions footer — admin only */}
      {isAdmin && (
      <div style={{
        padding: '10px 14px',
        borderTop: '1px solid var(--border)',
        display: 'flex', flexDirection: 'column', gap: 7,
      }}>
        {/* Admission controls — context-sensitive on approval_status.
            Pending: needs an admit/decline decision. Approved: can be
            declined (e.g. decommissioning) or removed outright. Rejected:
            can be reversed (re-approved) or removed outright. Remove is
            irreversible (DELETE), so it always asks for confirmation. */}
        {node.approvalStatus === 'pending' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
            <div style={{ display: 'flex', gap: 7 }}>
              <button
                className="btn btn-primary"
                style={{ flex: 1 }}
                onClick={() => onApprove?.(node.id)}
                title="Admit this node into the active array — it will be polled, mapped, and included in TDOA"
              >
                Approve
              </button>
              <button
                className="btn"
                style={{ flex: 1, borderColor: 'var(--red)', color: 'var(--red)' }}
                onClick={() => onReject?.(node.id)}
                title="Decline this node — keeps it out of the active array (reversible later)"
              >
                Reject
              </button>
            </div>
            <button
              className="btn"
              style={{ width: '100%' }}
              onClick={() => {
                if (window.confirm(`Remove ${node.hostname} permanently? This cannot be undone — the node will need to be re-discovered from scratch.`)) {
                  onRemove?.(node.id)
                }
              }}
              title="Permanently delete this node from the registry — skips the reject step"
            >
              Remove
            </button>
          </div>
        )}
        {node.approvalStatus === 'approved' && (
          <div style={{ display: 'flex', gap: 7 }}>
            <button
              className="btn"
              style={{ flex: 1, borderColor: 'var(--red)', color: 'var(--red)' }}
              onClick={() => onReject?.(node.id)}
              title="Decline this node — pulls it out of polling/map/TDOA (reversible later)"
            >
              Reject
            </button>
            <button
              className="btn"
              style={{ flex: 1 }}
              onClick={() => {
                if (window.confirm(`Remove ${node.hostname} permanently? This cannot be undone — the node will need to be re-discovered from scratch.`)) {
                  onRemove?.(node.id)
                }
              }}
              title="Permanently delete this node from the registry"
            >
              Remove
            </button>
          </div>
        )}
        {node.approvalStatus === 'rejected' && (
          <div style={{ display: 'flex', gap: 7 }}>
            <button
              className="btn btn-primary"
              style={{ flex: 1 }}
              onClick={() => onApprove?.(node.id)}
              title="Reverse the rejection — admit this node into the active array"
            >
              Re-approve
            </button>
            <button
              className="btn"
              style={{ flex: 1 }}
              onClick={() => {
                if (window.confirm(`Remove ${node.hostname} permanently? This cannot be undone — the node will need to be re-discovered from scratch.`)) {
                  onRemove?.(node.id)
                }
              }}
              title="Permanently delete this node from the registry"
            >
              Remove
            </button>
          </div>
        )}

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
        {node.role !== 'BROKER' && !node.positionKnown && (
          <button className="btn" style={{ width: '100%', borderColor: 'var(--yellow)', color: 'var(--yellow)' }}>
            Begin TOF Calibration
          </button>
        )}
        {node.role !== 'BROKER' && (
        <button
          className="btn"
          style={{ width: '100%' }}
          onClick={() => setPositionOpen(true)}
          title="Set this node's position in the hub's position database"
        >
          Set Position
        </button>
        )}
        <button
          className="btn"
          style={{ width: '100%' }}
          onClick={() => setConfigOpen(true)}
          title="Configure node-resident settings (broker mode)"
        >
          Configure
        </button>
      </div>
      )}


      {configOpen && (
        <NodeConfigModal
          node={node}
          onClose={() => setConfigOpen(false)}
          onSubmit={async (patch) => {
            await onConfigure?.(node.id, patch)
            setConfigOpen(false)
          }}
        />
      )}

      {positionOpen && (
        <NodePositionModal
          node={node}
          onClose={() => setPositionOpen(false)}
          onSubmit={async (pos) => {
            await onSetPosition?.(node.id, pos)
          }}
        />
      )}
    </div>
  )
}
