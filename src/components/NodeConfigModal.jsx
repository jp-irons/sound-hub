import { useState, useEffect } from 'react'
import { apiFetch } from '../auth.js'

export default function NodeConfigModal({ node, onClose, onSubmit, onSetSentinel }) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  const [isBroker, setIsBroker] = useState(false)
  const [selfTrigger, setSelfTrigger] = useState(true)
  // Hub-only bookkeeping (SENTINEL-MODE-PLAN.md Track B2) — unlike
  // isBroker/selfTrigger above, this never round-trips to the node, so its
  // current value comes from the node prop (NodeView.sentinel) rather than
  // the /config fetch below, which only ever reflects what's on the node
  // itself.
  const [sentinel, setSentinel] = useState(false)
  const [initial, setInitial] = useState(null)

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
    apiFetch(`/nodes/${node.id}/config`)
      .then(async res => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
        return res.json()
      })
      .then(cfg => {
        if (cancelled) return
        // selfTrigger defaults true if absent (matches the firmware's own
        // NodeConfig default) — a node not yet carrying this field should
        // read as "self-triggering as normal", not "off". sentinel isn't in
        // this response at all (see this component's sentinel state
        // comment) — read it off the node prop instead, folded into the
        // same `initial` snapshot so both baselines reset together whenever
        // node.id changes.
        const next = {
          isBroker: !!cfg.isBroker,
          selfTrigger: cfg.selfTrigger !== false,
          sentinel: !!node.sentinel,
        }
        setIsBroker(next.isBroker)
        setSelfTrigger(next.selfTrigger)
        setSentinel(next.sentinel)
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

    const configPatch = {}
    if (isBroker !== initial.isBroker) configPatch.isBroker = isBroker
    if (selfTrigger !== initial.selfTrigger) configPatch.selfTrigger = selfTrigger
    const sentinelChanged = sentinel !== initial.sentinel

    // Only submit if something actually changed.
    if (Object.keys(configPatch).length === 0 && !sentinelChanged) {
      onClose()
      return
    }

    setSubmitting(true)
    try {
      // Two independent writes, fired only for the fields that actually
      // changed. Sentinel is hub-only bookkeeping (works even while the
      // node is unreachable) and must never be gated on, or rolled back
      // by, a failure in the other — isBroker/selfTrigger proxy to the
      // node itself and do need it reachable. Neither depends on the
      // other succeeding, so order between them doesn't matter.
      if (Object.keys(configPatch).length > 0) {
        await onSubmit(configPatch)
      }
      if (sentinelChanged) {
        await onSetSentinel(sentinel)
      }
      onClose()
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
    >
      <div
        style={{
          width: 340, maxWidth: 'calc(100vw - 32px)', maxHeight: '85vh', overflowY: 'auto',
          background: 'var(--bg-panel)', border: '1px solid var(--border)',
          borderRadius: 8, padding: 18,
          display: 'flex', flexDirection: 'column', gap: 14,
        }}
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

            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={selfTrigger}
                onChange={e => setSelfTrigger(e.target.checked)}
              />
              <span>Self-trigger — pushes audio to the hub when AudioTrigger fires</span>
            </label>

            {/* Separated from the two checkboxes above — those are node-
                resident settings proxied over HTTP (need the node up to
                take); this one is hub-side bookkeeping only (SENTINEL-MODE-
                PLAN.md Track B2) and takes effect immediately regardless of
                whether the node is reachable right now. Independent of
                Self-trigger above by design — see the plan's four-
                combination table. */}
            <div style={{ borderTop: '1px solid var(--border-muted)', paddingTop: 12, display: 'flex', flexDirection: 'column', gap: 6 }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={sentinel}
                  onChange={e => setSentinel(e.target.checked)}
                />
                <span>Sentinel — hub continuously pulls &amp; analyzes audio from this node</span>
              </label>
              <div style={{ fontSize: 11, color: 'var(--text-muted)', paddingLeft: 24 }}>
                Hub-side only — applies immediately, independent of Self-trigger, and works even while this node is offline.
              </div>
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
