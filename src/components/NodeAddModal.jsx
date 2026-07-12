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
// POST /api/nodes/manual. Reachability is best-effort: the node is added
// either way, so a node that isn't deployed/powered on yet can be
// pre-provisioned — onSubmit resolves with the added node, and if it isn't
// reachable yet we show a note instead of just closing, so it's clear the
// add succeeded rather than silently doing nothing.
export default function NodeAddModal({ onClose, onSubmit }) {
  const [host, setHost] = useState('')
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [addedOffline, setAddedOffline] = useState(false)

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
      const node = await onSubmit(trimmed)
      if (node && !node.reachable) {
        setAddedOffline(true)
      } else {
        onClose()
      }
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

        {addedOffline ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div style={{
              fontSize: 12, color: 'var(--yellow)',
              background: 'var(--yellow-dim)', borderRadius: 4, padding: '8px 10px',
            }}>
              Added — not yet reachable. It'll show live status once it comes online.
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
              <button type="button" className="btn btn-primary" onClick={onClose}>
                Close
              </button>
            </div>
          </div>
        ) : (
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
                The hub will try reaching this node's own status API to seed its
                live status. If it's not reachable right now — e.g. pre-provisioning
                a node that isn't deployed yet — it's still added, keyed by whatever
                you type here. Prefer its hostname over its IP if you know it: that's
                what a future self-registration would use as its identity too, so
                they'll converge into the same node instead of creating a duplicate.
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
        )}
      </div>
    </div>
  )
}
