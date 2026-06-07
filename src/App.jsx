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

  const selectedNode = nodes.find(n => n.id === selectedId) ?? null
  const onlineCount = nodes.filter(n => n.status === 'online').length

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      <TopBar
        totalNodes={nodes.length}
        onlineCount={onlineCount}
        nodes={nodes}
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
