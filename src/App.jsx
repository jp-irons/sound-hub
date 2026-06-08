import { useState, useEffect, useCallback } from 'react'
import TopBar from './components/TopBar.jsx'
import NodeSidebar from './components/NodeSidebar.jsx'
import MapView from './components/MapView.jsx'
import NodeDetail from './components/NodeDetail.jsx'

// FastAPI backend — see server/main.py. Runs on the same machine as the
// Vite dev server during development (CORS is opened for localhost:5173).
const API_BASE = 'http://localhost:8000/api'

// Matches the backend's own poll interval (server/config.py
// STATUS_POLL_INTERVAL_S) — no point refreshing faster than the data changes.
const POLL_INTERVAL_MS = 5000

export default function App() {
  const [nodes, setNodes] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [error, setError] = useState(null)
  const [loaded, setLoaded] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/nodes`)
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
      const data = await res.json()
      setNodes(data)
      setError(null)
    } catch (err) {
      setError(err.message ?? String(err))
    } finally {
      setLoaded(true)
    }
  }, [])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [refresh])

  // Node admission/removal actions — POST to the approval endpoints or
  // DELETE the node, then re-pull the list so the UI reflects the new
  // state immediately rather than waiting for the next poll tick.
  const approveNode = useCallback(async (id) => {
    try {
      const res = await fetch(`${API_BASE}/nodes/${id}/approve`, { method: 'POST' })
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
      await refresh()
    } catch (err) {
      setError(err.message ?? String(err))
    }
  }, [refresh])

  const rejectNode = useCallback(async (id) => {
    try {
      const res = await fetch(`${API_BASE}/nodes/${id}/reject`, { method: 'POST' })
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
      await refresh()
    } catch (err) {
      setError(err.message ?? String(err))
    }
  }, [refresh])

  const removeNode = useCallback(async (id) => {
    try {
      const res = await fetch(`${API_BASE}/nodes/${id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
      setSelectedId(curr => (curr === id ? null : curr))
      await refresh()
    } catch (err) {
      setError(err.message ?? String(err))
    }
  }, [refresh])

  // Unlike the admission actions above, this one re-throws on failure —
  // the config modal needs the actual error message (e.g. the backend's
  // 409 "origin already set on <other node>") to show the operator,
  // rather than having it swallowed into the global error banner.
  const configureNode = useCallback(async (id, patch) => {
    const res = await fetch(`${API_BASE}/nodes/${id}/configure`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    })
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`
      try {
        const body = await res.json()
        if (body?.detail) detail = body.detail
      } catch { /* not JSON — fall back to status text */ }
      throw new Error(detail)
    }
    await refresh()
  }, [refresh])

  const selectedNode = nodes.find(n => n.id === selectedId) ?? null

  // Pending/rejected nodes aren't polled (see poller.py), so they'd always
  // read as "offline" — counting them here would just be misleading noise
  // in the top bar. Scope these summary counts to the active (approved) set.
  const approvedNodes = nodes.filter(n => n.approvalStatus === 'approved')
  const onlineCount = approvedNodes.filter(n => n.status === 'online').length

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      <TopBar
        totalNodes={approvedNodes.length}
        onlineCount={onlineCount}
        nodes={approvedNodes}
      />
      {error && (
        <div style={{
          padding: '6px 14px',
          background: 'var(--red-dim, rgba(219,68,55,0.15))',
          color: 'var(--red)',
          fontSize: 12,
        }}>
          Could not reach the base station API at {API_BASE} — {error}.
          Is the backend running ({"uvicorn server.main:app --reload --port 8000"})?
        </div>
      )}
      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        <NodeSidebar
          nodes={nodes}
          selectedId={selectedId}
          onSelect={setSelectedId}
          onApprove={approveNode}
          onReject={rejectNode}
        />
        <MapView
          nodes={nodes}
          selectedId={selectedId}
          onSelect={setSelectedId}
        />
        {selectedNode && (
          <NodeDetail
            node={selectedNode}
            onClose={() => setSelectedId(null)}
            onApprove={approveNode}
            onReject={rejectNode}
            onRemove={removeNode}
            onConfigure={configureNode}
          />
        )}
      </div>
      {loaded && nodes.length === 0 && !error && (
        <div style={{
          position: 'absolute', bottom: 14, left: 14,
          fontSize: 12, color: 'var(--text-muted)',
        }}>
          No nodes registered yet — discovery runs automatically via mDNS, or add one manually from the backend.
        </div>
      )}
    </div>
  )
}
