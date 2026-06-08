import { useState, useEffect } from 'react'

const API_BASE = 'http://localhost:8000/api'

const ROLES = [
  { value: 'primary', label: 'Primary' },
  { value: 'node',    label: 'Node' },
  { value: 'remote',  label: 'Remote' },
]

const POSITION_STATUSES = [
  { value: 'surveyed',  label: 'Surveyed' },
  { value: 'estimated', label: 'Estimated' },
]

function Field({ label, children }) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 }}>
      <span style={{ color: 'var(--text-secondary)' }}>{label}</span>
      {children}
    </label>
  )
}

const inputStyle = {
  background: 'var(--bg-input)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  color: 'var(--text-primary)',
  fontSize: 12,
  padding: '6px 8px',
  fontFamily: 'monospace',
  // Number inputs default to a browser-chosen intrinsic width that's wider
  // than the grid column gives them — without these, they overflow the
  // dialog rather than shrinking to fit.
  width: '100%',
  minWidth: 0,
  boxSizing: 'border-box',
}

const selectStyle = { ...inputStyle, fontFamily: 'inherit' }

// Numeric inputs hand back strings — keep them as strings while editing so
// the user can clear/retype freely, and only coerce to float on submit.
function NumberField({ label, value, onChange, step = 'any' }) {
  return (
    <Field label={label}>
      <input
        type="number"
        step={step}
        value={value}
        onChange={e => onChange(e.target.value)}
        style={inputStyle}
      />
    </Field>
  )
}

export default function NodeConfigModal({ node, onClose, onSubmit }) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  // Form state — strings throughout (incl. numbers) so inputs stay
  // controlled and editable; coerced to the right types on submit.
  const [role, setRole] = useState('node')
  const [posE, setPosE] = useState('')
  const [posN, setPosN] = useState('')
  const [posAlt, setPosAlt] = useState('')
  const [positionStatus, setPositionStatus] = useState('estimated')
  const [isOrigin, setIsOrigin] = useState(false)
  const [originLat, setOriginLat] = useState('')
  const [originLon, setOriginLon] = useState('')
  const [originAlt, setOriginAlt] = useState('')

  // Track the as-loaded values so we only submit fields the operator
  // actually changed — mirrors the backend's exclude_unset proxy, and
  // avoids clobbering fields the form merely displayed.
  const [initial, setInitial] = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    fetch(`${API_BASE}/nodes/${node.id}/config`)
      .then(async res => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
        return res.json()
      })
      .then(cfg => {
        if (cancelled) return
        const next = {
          role: cfg.role ?? 'node',
          posE: cfg.posE ?? '',
          posN: cfg.posN ?? '',
          posAlt: cfg.posAlt ?? '',
          positionStatus: cfg.positionStatus ?? 'estimated',
          isOrigin: !!cfg.isOrigin,
          originLat: cfg.originLat ?? '',
          originLon: cfg.originLon ?? '',
          originAlt: cfg.originAlt ?? '',
        }
        setRole(next.role)
        setPosE(String(next.posE))
        setPosN(String(next.posN))
        setPosAlt(String(next.posAlt))
        setPositionStatus(next.positionStatus)
        setIsOrigin(next.isOrigin)
        setOriginLat(String(next.originLat))
        setOriginLon(String(next.originLon))
        setOriginAlt(String(next.originAlt))
        setInitial(next)
      })
      .catch(err => !cancelled && setError(err.message ?? String(err)))
      .finally(() => !cancelled && setLoading(false))
    return () => { cancelled = true }
  }, [node.id])

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!initial) return
    setError(null)

    // Build a patch with only the fields that differ from what we loaded.
    const current = {
      role,
      posE: posE === '' ? null : parseFloat(posE),
      posN: posN === '' ? null : parseFloat(posN),
      posAlt: posAlt === '' ? null : parseFloat(posAlt),
      positionStatus,
      isOrigin,
      originLat: originLat === '' ? null : parseFloat(originLat),
      originLon: originLon === '' ? null : parseFloat(originLon),
      originAlt: originAlt === '' ? null : parseFloat(originAlt),
    }

    const patch = {}
    for (const key of Object.keys(current)) {
      const before = initial[key]
      const after = current[key]
      const changed = typeof after === 'number'
        ? Number(before) !== after
        : before !== after
      if (changed && after !== null && !(typeof after === 'number' && Number.isNaN(after))) {
        patch[key] = after
      }
    }

    if (Object.keys(patch).length === 0) {
      onClose()
      return
    }

    setSubmitting(true)
    try {
      await onSubmit(patch)
    } catch (err) {
      setError(err.message ?? String(err))
      setSubmitting(false)
    }
  }

  return (
    <div
      style={{
        // Leaflet's own controls/panes (in MapView) sit at z-index 1000,
        // and MapView has a custom overlay badge at the same — the modal
        // needs to clear both or it renders invisibly behind the map.
        position: 'fixed', inset: 0, zIndex: 2000,
        background: 'rgba(0,0,0,0.55)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
      onClick={onClose}
    >
      <div
        style={{
          width: 380, maxHeight: '85vh', overflowY: 'auto',
          background: 'var(--bg-panel)', border: '1px solid var(--border)',
          borderRadius: 8, padding: 18,
          display: 'flex', flexDirection: 'column', gap: 14,
        }}
        onClick={e => e.stopPropagation()}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ fontWeight: 700, fontSize: 14, flex: 1 }}>
            Configure {node.hostname}
          </div>
          <button
            onClick={onClose}
            style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', fontSize: 16, lineHeight: 1 }}
            title="Close"
          >×</button>
        </div>

        {loading ? (
          <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>Loading current config…</div>
        ) : (
          <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <Field label="Role">
              <select value={role} onChange={e => setRole(e.target.value)} style={selectStyle}>
                {ROLES.map(r => <option key={r.value} value={r.value}>{r.label}</option>)}
              </select>
            </Field>

            <div className="section-label" style={{ marginBottom: 0 }}>Position (relative E/N/Alt)</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1fr)', gap: 8 }}>
              <NumberField label="E (m)" value={posE} onChange={setPosE} />
              <NumberField label="N (m)" value={posN} onChange={setPosN} />
              <NumberField label="Alt (m)" value={posAlt} onChange={setPosAlt} />
            </div>

            <Field label="Position status">
              <select value={positionStatus} onChange={e => setPositionStatus(e.target.value)} style={selectStyle}>
                {POSITION_STATUSES.map(s => <option key={s.value} value={s.value}>{s.label}</option>)}
              </select>
            </Field>
            <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: -8 }}>
              "Surveyed" marks this E/N/Alt as a confirmed ground-truth anchor;
              "Estimated" leaves it open to refinement by ongoing calibration.
            </div>

            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
              <input type="checkbox" checked={isOrigin} onChange={e => setIsOrigin(e.target.checked)} />
              <span>Is origin (geometric anchor for the array)</span>
            </label>
            <div style={{ fontSize: 11, color: 'var(--yellow)', marginTop: -8 }}>
              Only one node in the array should be flagged as origin — the
              hub will reject this change if another node already is. Clear
              it there first if you're moving the origin.
            </div>

            {isOrigin && (
              <>
                <div className="section-label" style={{ marginBottom: 0 }}>Surveyed origin (lat/lon/alt)</div>
                <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', gap: 8 }}>
                  <NumberField label="Latitude" value={originLat} onChange={setOriginLat} step="0.0000001" />
                  <NumberField label="Longitude" value={originLon} onChange={setOriginLon} step="0.0000001" />
                </div>
                <NumberField label="Altitude (m)" value={originAlt} onChange={setOriginAlt} step="0.01" />
              </>
            )}

            {error && (
              <div style={{
                fontSize: 12, color: 'var(--red)',
                background: 'var(--red-dim)', borderRadius: 4, padding: '6px 8px',
              }}>
                {error}
              </div>
            )}

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button type="button" className="btn" onClick={onClose} disabled={submitting}>
                Cancel
              </button>
              <button type="submit" className="btn btn-primary" disabled={submitting}>
                {submitting ? 'Saving…' : 'Save'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  )
}
