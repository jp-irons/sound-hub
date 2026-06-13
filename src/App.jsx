import { useState, useEffect, useCallback } from 'react'
import TopBar from './components/TopBar.jsx'
import NodeSidebar from './components/NodeSidebar.jsx'
import MapView from './components/MapView.jsx'
import NodeDetail from './components/NodeDetail.jsx'
import DetectionsTab from './components/DetectionsTab.jsx'
import AuthOverlay from './components/AuthOverlay.jsx'
import UsersTab from './components/UsersTab.jsx'
import { apiFetch, setToken, clearToken, onUnauthenticated, AuthError } from './auth.js'

const API_BASE = 'http://localhost:8000/api'
const POLL_INTERVAL_MS = 5000

export default function App() {
  // 'login' | 'setup' | 'authenticated'
  const [authState, setAuthState] = useState('login')
  const [user, setUser] = useState(null)   // { username, role }

  const [tab, setTab] = useState('nodes')
  const [nodes, setNodes] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [error, setError] = useState(null)
  const [loaded, setLoaded] = useState(false)

  // Wire up the 401 callback so any apiFetch can flip us back to login.
  useEffect(() => {
    onUnauthenticated(() => {
      setUser(null)
      setAuthState('login')
    })
  }, [])

  // On mount, check whether first-run setup is needed.
  // authState starts as 'login' so the overlay is visible immediately;
  // we only switch to 'setup' if the backend says no users exist yet.
  useEffect(() => {
    async function checkAuth() {
      try {
        const res = await fetch(`${API_BASE}/auth/status`)
        const body = await res.json()
        if (body.setup_required) setAuthState('setup')
        // else leave as 'login' — already correct
      } catch {
        // Backend unreachable — leave as 'login', error will surface on submit.
      }
    }
    checkAuth()
  }, [])

  function handleAuthSuccess(token, username, role) {
    setToken(token)
    setUser({ username, role })
    setAuthState('authenticated')
  }

  function handleLogout() {
    clearToken()
    setUser(null)
    setAuthState('login')
  }

  // -------------------------------------------------------------------------
  // Data fetching
  // -------------------------------------------------------------------------

  const refresh = useCallback(async () => {
    try {
      const res = await apiFetch('/nodes')
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
      const data = await res.json()
      setNodes(data)
      setError(null)
    } catch (err) {
      if (err instanceof AuthError) return   // overlay handles it
      setError(err.message ?? String(err))
    } finally {
      setLoaded(true)
    }
  }, [])

  useEffect(() => {
    if (authState !== 'authenticated') return
    refresh()
    const interval = setInterval(refresh, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [authState, refresh])

  // -------------------------------------------------------------------------
  // Node actions
  // -------------------------------------------------------------------------

  const approveNode = useCallback(async (id) => {
    try {
      const res = await apiFetch(`/nodes/${id}/approve`, { method: 'POST' })
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
      await refresh()
    } catch (err) {
      if (err instanceof AuthError) return
      setError(err.message ?? String(err))
    }
  }, [refresh])

  const rejectNode = useCallback(async (id) => {
    try {
      const res = await apiFetch(`/nodes/${id}/reject`, { method: 'POST' })
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
      await refresh()
    } catch (err) {
      if (err instanceof AuthError) return
      setError(err.message ?? String(err))
    }
  }, [refresh])

  const removeNode = useCallback(async (id) => {
    try {
      const res = await apiFetch(`/nodes/${id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
      setSelectedId(curr => (curr === id ? null : curr))
      await refresh()
    } catch (err) {
      if (err instanceof AuthError) return
      setError(err.message ?? String(err))
    }
  }, [refresh])

  const configureNode = useCallback(async (id, patch) => {
    const res = await apiFetch(`/nodes/${id}/configure`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    })
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`
      try {
        const body = await res.json()
        if (body?.detail) detail = body.detail
      } catch { /* not JSON */ }
      throw new Error(detail)
    }
    await refresh()
  }, [refresh])

  const setNodePosition = useCallback(async (id, position) => {
    const res = await apiFetch(`/nodes/${id}/position`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(position),
    })
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`
      try {
        const body = await res.json()
        if (body?.detail) detail = body.detail
      } catch { /* not JSON */ }
      throw new Error(detail)
    }
    await refresh()
  }, [refresh])

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  const selectedNode = nodes.find(n => n.id === selectedId) ?? null
  const approvedNodes = nodes.filter(n => n.approvalStatus === 'approved')
  const onlineCount = approvedNodes.filter(n => n.status === 'online').length

  const tabStyle = (t) => ({
    padding: '4px 16px',
    fontSize: 12,
    fontWeight: 500,
    cursor: 'pointer',
    border: 'none',
    borderBottom: tab === t ? '2px solid var(--accent, #4da6ff)' : '2px solid transparent',
    background: 'transparent',
    color: tab === t ? 'var(--accent, #4da6ff)' : 'var(--text-muted, #888)',
    transition: 'color 0.15s',
  })

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>

      {/* Auth overlay — setup or login */}
      {(authState === 'setup' || authState === 'login') && (
        <AuthOverlay mode={authState} onSuccess={handleAuthSuccess} />
      )}

      <TopBar
        totalNodes={approvedNodes.length}
        onlineCount={onlineCount}
        nodes={approvedNodes}
        user={user}
        onLogout={handleLogout}
        onSignIn={() => setAuthState('login')}
      />

      {/* Tab bar */}
      <div style={{
        display: 'flex', alignItems: 'center',
        borderBottom: '1px solid var(--border, #333)',
        background: 'var(--surface1, #1e1e1e)',
        paddingLeft: 8,
        flexShrink: 0,
      }}>
        <button style={tabStyle('nodes')}      onClick={() => setTab('nodes')}>Nodes</button>
        <button style={tabStyle('detections')} onClick={() => setTab('detections')}>Detections</button>
        {user?.role === 'admin' && (
          <button style={tabStyle('users')} onClick={() => setTab('users')}>Users</button>
        )}
      </div>

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

      {tab === 'nodes' && (
        <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
          <NodeSidebar
            nodes={nodes}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onApprove={approveNode}
            onReject={rejectNode}
          />
          <MapView nodes={approvedNodes} onSelectNode={setSelectedId} />
          {selectedNode && (
            <NodeDetail
              node={selectedNode}
              onClose={() => setSelectedId(null)}
              onRemove={removeNode}
              onConfigure={configureNode}
              onSetPosition={setNodePosition}
            />
          )}
        </div>
      )}

      {tab === 'detections' && <DetectionsTab />}
      {tab === 'users'      && <UsersTab user={user} />}
    </div>
  )
}
