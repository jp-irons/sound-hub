import { useState, useEffect } from 'react'

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

// Manual add — the fallback discovery path now that mDNS is retired (see
// project memory `project-mdns-to-dns-migration`). Hits the backend's
// POST /api/nodes/manual, which reaches out to the node's own status API to
// validate it before adding — so this only succeeds against a node that's
// actually reachable right now.
export default function NodeAddModal({ onClose, onSubmit }) {
  const [host, setHost] = useState('')
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    function handleKey(e) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose])

  const trimmed = host.trim()

  const handleSubmit = async () => {
    if (!trimmed) return
    setError(null)
    setSubmitting(true)
    try {
      await onSubmit(trimmed)
      onClose()
    } catch (err) {
      setError(err.message ?? String(err))
    } finally {
      setSubmitting(false)
    }
  }

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
          width: 360, maxWidth: 'calc(100vw - 32px)',
          background: 'var(--bg-panel)', border: '1px solid var(--border)',
          borderRadius: 8, padding: 18,
          display: 'flex', flexDirection: 'column', gap: 14,
        }}
      >
        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ fontWeight: 700, fontSize: 14, flex: 1 }}>
            Add Node
          </div>
          <button
            onClick={onClose}
            style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', fontSize: 16, lineHeight: 1 }}
            title="Close"
          >×</button>
        </div>

        <form onSubmit={e => { e.preventDefault(); handleSubmit() }} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 }}>
            <span style={{ color: 'var(--text-secondary)' }}>Hostname or IP</span>
            <input
              type="text"
              autoFocus
              value={host}
              placeholder="e.g. 192.168.101.150"
              onChange={e => setHost(e.target.value)}
              style={inputStyle}
            />
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
              The hub will reach out to this node's own status API to confirm it's
              there before adding it — it must be reachable right now.
            </span>
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
            <button
              type="submit" className="btn btn-primary"
              disabled={submitting || !trimmed}
            >
              {submitting ? 'Adding…' : 'Add'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
