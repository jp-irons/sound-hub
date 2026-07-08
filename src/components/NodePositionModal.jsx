import { useState, useEffect } from 'react'
import { apiFetch } from '../auth.js'

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

export default function NodePositionModal({ node, onClose, onSubmit }) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [settingOrigin, setSettingOrigin] = useState(false)
  const [originMessage, setOriginMessage] = useState(null)
  const [emaLoading, setEmaLoading] = useState(false)
  const [emaMessage, setEmaMessage] = useState(null)

  // Form state — strings so inputs stay controlled; coerced on submit.
  const [posE, setPosE] = useState('')
  const [posN, setPosN] = useState('')
  const [posAlt, setPosAlt] = useState('')
  const [posStatus, setPosStatus] = useState('estimated')

  const disagreementM = node.surveyDisagreementM

  // Escape closes — backdrop click does not (a stray click while
  // refocusing the window, or a text-selection drag ending outside the
  // modal, must not dismiss it).
  useEffect(() => {
    function handleKey(e) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    apiFetch(`/nodes/${node.id}/position`)
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
      })
      .catch(err => !cancelled && setError(err.message ?? String(err)))
      .finally(() => !cancelled && setLoading(false))
    return () => { cancelled = true }
  }, [node.id])

  const handleSubmit = async (andClose) => {
    setError(null)
    const payload = {
      posE:   posE   === '' ? null : parseFloat(posE),
      posN:   posN   === '' ? null : parseFloat(posN),
      posAlt: posAlt === '' ? null : parseFloat(posAlt),
      posStatus,
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

  // Preview-only: fetches the node's current hub-side GPS EMA back-projected
  // through the array origin and fills the N/E/Alt fields with it. Nothing
  // is persisted until the operator hits Apply/Save & Close below — same
  // as typing the numbers in by hand.
  const handleUseLiveEma = async () => {
    setEmaLoading(true)
    setEmaMessage(null)
    setError(null)
    try {
      const res = await apiFetch(`/nodes/${node.id}/position/from-ema`)
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail ?? `${res.status} ${res.statusText}`)
      }
      const preview = await res.json()
      setPosN(preview.posN.toFixed(2))
      setPosE(preview.posE.toFixed(2))
      setPosAlt(preview.posAlt.toFixed(2))
      setEmaMessage(
        `Filled from live EMA (${preview.emaN.toLocaleString()} samples) — review, then Apply/Save to persist`
      )
    } catch (err) {
      setError(err.message ?? String(err))
    } finally {
      setEmaLoading(false)
    }
  }

  const handleSetOrigin = async () => {
    setSettingOrigin(true)
    setOriginMessage(null)
    setError(null)
    try {
      const res = await apiFetch(`/origin/set-from-node/${node.id}`, { method: 'POST' })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail ?? `${res.status} ${res.statusText}`)
      }
      const origin = await res.json()
      setOriginMessage(
        `Origin set from live GPS EMA: ${origin.lat.toFixed(6)}, ${origin.lon.toFixed(6)}, ${origin.altM?.toFixed(1) ?? '?'} m`
      )
    } catch (err) {
      setError(err.message ?? String(err))
    } finally {
      setSettingOrigin(false)
    }
  }

  const offsetFilled = posE !== '' && posN !== '' && posAlt !== ''
  const isSurveyed = posStatus === 'surveyed'
  const canSetOrigin = isSurveyed && offsetFilled

  return (
    <div
      style={{
        position: 'fixed', inset: 0, zIndex: 2000,
        background: 'rgba(0,0,0,0.55)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div
        style={{
          width: 420, maxWidth: 'calc(100vw - 32px)', maxHeight: '90vh', overflowY: 'auto',
          background: 'var(--bg-panel)', border: '1px solid var(--border)',
          borderRadius: 8, padding: 18,
          display: 'flex', flexDirection: 'column', gap: 14,
        }}
      >
        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ fontWeight: 700, fontSize: 14, flex: 1 }}>
            Set Position — {node.hostname}
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
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 0 }}>
              <div className="section-label" style={{ marginBottom: 0, flex: 1 }}>
                Array position (N / E / Alt from origin)
              </div>
              <button
                type="button"
                className="btn"
                style={{ fontSize: 10, padding: '2px 8px' }}
                disabled={emaLoading}
                title="Fill N/E/Alt below from this node's current hub-side GPS EMA, back-projected through the array origin — preview only, not saved until you Apply"
                onClick={handleUseLiveEma}
              >
                {emaLoading ? 'Loading…' : 'Use Live EMA'}
              </button>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: 8 }}>
              <NumberField label="N (m)" value={posN} onChange={setPosN} />
              <NumberField label="E (m)" value={posE} onChange={setPosE} />
              <NumberField label="Alt (m)" value={posAlt} onChange={setPosAlt} />
            </div>
            {emaMessage && (
              <div style={{ fontSize: 11, color: 'var(--green)', marginTop: -8 }}>{emaMessage}</div>
            )}

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
                Set hub origin from this node
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                Back-projects the hub's geographic origin from this node's current live GPS EMA
                minus its N/E/Alt offset (above). All other surveyed nodes remain valid — no
                re-surveying required. If you have an independent absolute reference for the
                origin itself (e.g. a survey-grade GNSS reading or a known landmark), set it
                directly from the Settings tab instead.
              </div>

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

              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <button
                  type="button"
                  className="btn btn-primary"
                  style={{ fontSize: 11, padding: '5px 12px' }}
                  disabled={!canSetOrigin || settingOrigin}
                  title={canSetOrigin
                    ? "Back-project hub origin from this node's live GPS EMA + N/E/Alt offset"
                    : 'Requires: Surveyed status and all three offsets entered above'}
                  onClick={handleSetOrigin}
                >
                  {settingOrigin ? 'Setting…' : 'Set hub origin'}
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
