import { useState, useEffect } from 'react'

const API_BASE = '/api'

const POSITION_STATUSES = [
  { value: 'estimated', label: 'Estimated' },
  { value: 'surveyed',  label: 'Surveyed' },
]

function Field({ label, hint, children }) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 }}>
      <span style={{ color: 'var(--text-secondary)' }}>{label}</span>
      {children}
      {hint && <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{hint}</span>}
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
  width: '100%',
  minWidth: 0,
  boxSizing: 'border-box',
}

const selectStyle = { ...inputStyle, fontFamily: 'inherit' }

function NumberField({ label, value, onChange, step = 'any', placeholder }) {
  return (
    <Field label={label}>
      <input
        type="number"
        step={step}
        value={value}
        placeholder={placeholder}
        onChange={e => onChange(e.target.value)}
        style={inputStyle}
      />
    </Field>
  )
}

function fmtLatLon(v, decimals = 6) {
  return v != null ? v.toFixed(decimals) : '—'
}

function fmtM(v) {
  return v != null ? `${v.toFixed(1)} m` : '—'
}

export default function NodePositionModal({ node, onClose, onSubmit }) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [originSetting, setOriginSetting] = useState(null)  // null | 'gps_centroid' | 'surveyed_coords'
  const [originMessage, setOriginMessage] = useState(null)

  // Form state — strings so inputs stay controlled; coerced on submit.
  const [posE, setPosE] = useState('')
  const [posN, setPosN] = useState('')
  const [posAlt, setPosAlt] = useState('')
  const [posStatus, setPosStatus] = useState('estimated')
  const [surveyedLat, setSurveyedLat] = useState('')
  const [surveyedLon, setSurveyedLon] = useState('')
  const [surveyedAlt, setSurveyedAlt] = useState('')

  const centroid = node.gps?.centroid ?? null
  const disagreementM = node.surveyDisagreementM
  const isPosRef = node.isOrigin

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    fetch(`${API_BASE}/nodes/${node.id}/position`)
      .then(async res => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
        return res.json()
      })
      .then(pos => {
        if (cancelled) return
        setPosE(pos.posE != null ? String(pos.posE) : '')
        setPosN(pos.posN != null ? String(pos.posN) : '')
        setPosAlt(pos.posAlt != null ? String(pos.posAlt) : '')
        setPosStatus(pos.posStatus ?? 'estimated')
        setSurveyedLat(pos.surveyedLat != null ? String(pos.surveyedLat) : '')
        setSurveyedLon(pos.surveyedLon != null ? String(pos.surveyedLon) : '')
        setSurveyedAlt(pos.surveyedAlt != null ? String(pos.surveyedAlt) : '')
      })
      .catch(err => !cancelled && setError(err.message ?? String(err)))
      .finally(() => !cancelled && setLoading(false))
    return () => { cancelled = true }
  }, [node.id])

  const handleSubmit = async (andClose) => {
    setError(null)
    const payload = {
      posE:        posE        === '' ? null : parseFloat(posE),
      posN:        posN        === '' ? null : parseFloat(posN),
      posAlt:      posAlt      === '' ? null : parseFloat(posAlt),
      posStatus,
      isOrigin:    isPosRef,
      surveyedLat: surveyedLat === '' ? null : parseFloat(surveyedLat),
      surveyedLon: surveyedLon === '' ? null : parseFloat(surveyedLon),
      surveyedAlt: surveyedAlt === '' ? null : parseFloat(surveyedAlt),
    }
    setSubmitting(true)
    try {
      await onSubmit(payload)
      if (andClose) onClose()
    } catch (err) {
      setError(err.message ?? String(err))
    } finally {
      setSubmitting(false)
    }
  }

  const handleSetOrigin = async (source) => {
    setOriginSetting(source)
    setOriginMessage(null)
    setError(null)
    try {
      const url = `${API_BASE}/origin/set-from-node/${node.id}?source=${source}`
      const res = await fetch(url, { method: 'POST' })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail ?? `${res.status} ${res.statusText}`)
      }
      const origin = await res.json()
      const srcLabel = source === 'surveyed_coords' ? 'surveyed coordinates' : 'GPS centroid'
      setOriginMessage(
        `Origin set from ${srcLabel}: ${origin.lat.toFixed(6)}, ${origin.lon.toFixed(6)}, ${origin.altM?.toFixed(1) ?? '?'} m`
      )
    } catch (err) {
      setError(err.message ?? String(err))
    } finally {
      setOriginSetting(null)
    }
  }

  const offsetFilled = posE !== '' && posN !== '' && posAlt !== ''
  const isSurveyed = posStatus === 'surveyed'
  const canUseGpsCentroid = isSurveyed && offsetFilled && centroid != null
  const canUseSurveyedCoords = isSurveyed && offsetFilled &&
    surveyedLat !== '' && surveyedLon !== '' && surveyedAlt !== ''

  return (
    <div
      style={{
        position: 'fixed', inset: 0, zIndex: 2000,
        background: 'rgba(0,0,0,0.55)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
      onClick={onClose}
    >
      <div
        style={{
          width: 420, maxHeight: '90vh', overflowY: 'auto',
          background: 'var(--bg-panel)', border: '1px solid var(--border)',
          borderRadius: 8, padding: 18,
          display: 'flex', flexDirection: 'column', gap: 14,
        }}
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ fontWeight: 700, fontSize: 14, flex: 1 }}>
            Set Position — {node.hostname}
            {isPosRef && (
              <span style={{
                marginLeft: 8, fontSize: 10, fontWeight: 600,
                color: 'var(--blue)', background: 'var(--blue-dim)',
                borderRadius: 3, padding: '2px 6px', verticalAlign: 'middle',
              }}>
                POS REF
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', fontSize: 16, lineHeight: 1 }}
            title="Close"
          >×</button>
        </div>

        {loading ? (
          <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>Loading position…</div>
        ) : (
          <form onSubmit={e => e.preventDefault()} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>

            {/* Array-frame offset */}
            <div className="section-label" style={{ marginBottom: 0 }}>
              Array position (N / E / Alt from origin)
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: 8 }}>
              <NumberField label="N (m)" value={posN} onChange={setPosN} />
              <NumberField label="E (m)" value={posE} onChange={setPosE} />
              <NumberField label="Alt (m)" value={posAlt} onChange={setPosAlt} />
            </div>

            {/* Absolute surveyed coordinates */}
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 0 }}>
              <div className="section-label" style={{ marginBottom: 0, flex: 1 }}>
                Surveyed coordinates (optional)
              </div>
              {centroid && (
                <button
                  type="button"
                  className="btn"
                  style={{ fontSize: 10, padding: '2px 8px' }}
                  title="Populate from this node's GPS centroid"
                  onClick={() => {
                    setSurveyedLat(centroid.lat.toFixed(8))
                    setSurveyedLon(centroid.lon.toFixed(8))
                    setSurveyedAlt(centroid.altM != null ? String(centroid.altM) : '')
                  }}
                >
                  Copy GPS centroid
                </button>
              )}
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: 8 }}>
              <NumberField label="Latitude" value={surveyedLat} onChange={setSurveyedLat} placeholder="-27.123456" />
              <NumberField label="Longitude" value={surveyedLon} onChange={setSurveyedLon} placeholder="153.123456" />
              <NumberField label="Alt (m)" value={surveyedAlt} onChange={setSurveyedAlt} placeholder="42.0" />
            </div>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: -8 }}>
              Independently surveyed absolute position. Used as an alternative to GPS centroid when setting the array origin.
            </div>

            {/* Position status */}
            <Field
              label="Position status"
              hint={"\"Surveyed\" marks this position as confirmed ground truth. \"Estimated\" leaves it open to refinement."}
            >
              <select value={posStatus} onChange={e => setPosStatus(e.target.value)} style={selectStyle}>
                {POSITION_STATUSES.map(s => <option key={s.value} value={s.value}>{s.label}</option>)}
              </select>
            </Field>

            {/* Array origin */}
            <div style={{
              padding: '10px 12px',
              background: 'var(--bg-input)',
              border: '1px solid var(--border)',
              borderRadius: 6,
              display: 'flex', flexDirection: 'column', gap: 8,
            }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)' }}>
                Set as position reference
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                Back-projects the array (0,0,0) datum from this node's reference coordinates minus its N/E/Alt offset.
                All other surveyed nodes remain valid — no re-surveying required.
              </div>

              {/* GPS centroid info */}
              {centroid ? (
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                  GPS centroid: {fmtLatLon(centroid.lat)}, {fmtLatLon(centroid.lon)}, {fmtM(centroid.altM)}
                  {node.gps?.centroidN != null && (
                    <span style={{ marginLeft: 6 }}>
                      (n={node.gps.centroidN.toLocaleString()}
                      {node.gps.centroidStddevM != null && `, σ ${node.gps.centroidStddevM.toFixed(2)} m`})
                    </span>
                  )}
                </div>
              ) : (
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>GPS centroid: not yet available</div>
              )}

              {disagreementM != null && (
                <div style={{
                  fontSize: 11,
                  color: disagreementM < 2 ? 'var(--green)' : disagreementM < 5 ? 'var(--yellow)' : 'var(--red)',
                }}>
                  Survey disagreement: {disagreementM.toFixed(2)} m from stored position
                </div>
              )}

              {originMessage && (
                <div style={{ fontSize: 11, color: 'var(--green)' }}>{originMessage}</div>
              )}

              {/* Two origin buttons */}
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <button
                  type="button"
                  className="btn btn-primary"
                  style={{ fontSize: 11, padding: '5px 12px' }}
                  disabled={!canUseGpsCentroid || originSetting != null}
                  title={canUseGpsCentroid
                    ? 'Back-project origin from this node\'s GPS centroid + N/E/Alt offset'
                    : 'Requires: Surveyed status, all three offsets, and an active GPS centroid'}
                  onClick={() => handleSetOrigin('gps_centroid')}
                >
                  {originSetting === 'gps_centroid' ? 'Setting…' : 'Use GPS centroid'}
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  style={{ fontSize: 11, padding: '5px 12px' }}
                  disabled={!canUseSurveyedCoords || originSetting != null}
                  title={canUseSurveyedCoords
                    ? 'Back-project origin from the surveyed lat/lon/alt above + N/E/Alt offset'
                    : 'Requires: Surveyed status, all three offsets, and all three surveyed coordinates entered above'}
                  onClick={() => handleSetOrigin('surveyed_coords')}
                >
                  {originSetting === 'surveyed_coords' ? 'Setting…' : 'Use surveyed coordinates'}
                </button>
              </div>

              {!isSurveyed && (
                <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>
                  Position status must be set to Surveyed before setting as origin.
                </div>
              )}
            </div>

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
              <button
                type="button" className="btn btn-primary"
                disabled={submitting}
                onClick={() => handleSubmit(false)}
              >
                {submitting ? 'Saving…' : 'Apply'}
              </button>
              <button
                type="button" className="btn btn-primary"
                disabled={submitting}
                onClick={() => handleSubmit(true)}
              >
                {submitting ? 'Saving…' : 'Save & Close'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  )
}
