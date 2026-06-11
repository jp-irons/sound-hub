import { useState, useEffect } from 'react'

const API_BASE = 'http://localhost:8000/api'


export default function NodeConfigModal({ node, onClose, onSubmit }) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  const [isBroker, setIsBroker] = useState(false)
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
        const next = { isBroker: !!cfg.isBroker }
        setIsBroker(next.isBroker)
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

    // Only submit if the value actually changed.
    if (isBroker === initial.isBroker) {
      onClose()
      return
    }

    setSubmitting(true)
    try {
      await onSubmit({ isBroker })
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
          width: 340, maxHeight: '85vh', overflowY: 'auto',
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

            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={isBroker}
                onChange={e => setIsBroker(e.target.checked)}
              />
              <span>Broker — relays ESP-NOW traffic to/from WiFi</span>
            </label>

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
