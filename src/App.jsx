import { useState, useEffect, useCallback } from 'react'
import TopBar from './components/TopBar.jsx'
import NodeSidebar from './components/NodeSidebar.jsx'
import MapView from './components/MapView.jsx'
import NodeDetail from './components/NodeDetail.jsx'
import DetectionsTab from './components/DetectionsTab.jsx'
import AuthOverlay from './components/AuthOverlay.jsx'
import UsersTab from './components/UsersTab.jsx'
import { apiFetch, setToken, clearToken, onUnauthenticated, AuthError } from './auth.js'

const API_BASE = '/api'
const POLL_INTERVAL_MS = 5000

export default function App() {
  // 'login' | 'setup' | 'unauthenticated' | 'authenticated'
  const [authState, setAuthState] = useState('unauthenticated')
  const [user, setUser] = useState(null)   // { username, role }

  const [tab, setTab] = useState('map')
  const [nodes, setNodes] = useState([])             // full node list — authenticated only
  const [publicNodes, setPublicNodes] = useState([]) // slim node list — unauthenticated
  const [selectedId, setSelectedId] = useState(null)
  const [error, setError] = useState(null)

  const isAdmin = user?.role === 'admin'

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
        // else leave as 'unauthenticated' — user browses map and signs in on demand
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
    setNodes([])
    setAuthState('login')
  }

  // -------------------------------------------------------------------------
  // Data fetching
  // -------------------------------------------------------------------------

  // Authenticated polling — full node details
  const refresh = useCallback(async () => {
    try {
      const res = await apiFetch('/nodes')
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
      setNodes(await res.json())
      setError(null)
    } catch (err) {
      if (err instanceof AuthError) return
      setError(err.message ?? String(err))
    }
  }, [])

  useEffect(() => {
    if (authState !== 'authenticated') return
    refresh()
    const interval = setInterval(refresh, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [authState, refresh])

  // Unauthenticated polling — slim public nodes for map display
  const refreshPublic = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/public/nodes`)
      if (!res.ok) return
      setPublicNodes(await res.json())
    } catch {
      // Silently ignore — map just shows no nodes
    }
  }, [])

  useEffect(() => {
    if (authState !== 'unauthenticated') return
    refreshPublic()
    const interval = setInterval(refreshPublic, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [authState, refreshPublic])

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

  const approvedNodes = nodes.filter(n => n.approvalStatus === 'approved')
  const onlineCount = approvedNodes.filter(n => n.status === 'online').length
  const selectedNode = nodes.find(n => n.id === selectedId) ?? null

  // Nodes shown on the map depend on auth state
  const mapNodes = authState === 'authenticated' ? approvedNodes : publicNodes

  const showOverlay = authState === 'setup' || authState === 'login'

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
      {showOverlay && (
        <AuthOverlay
          mode={authState}
          onSuccess={handleAuthSuccess}
          onBrowse={() => setAuthState('unauthenticated')}
        />
      )}

      <TopBar
        totalNodes={authState === 'authenticated' ? approvedNodes.length : publicNodes.length}
        onlineCount={authState === 'authenticated' ? onlineCount : publicNodes.filter(n => n.status === 'online').length}
        nodes={authState === 'authenticated' ? approvedNodes : []}
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
        <button style={tabStyle('map')}        onClick={() => setTab('map')}>Map</button>
        <button style={tabStyle('detections')} onClick={() => setTab('detections')}>Detections</button>

        {isAdmin && (
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

      {tab === 'map' && (
        <div style={{ flex: 1, position: 'relative', overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
          <MapView
            nodes={mapNodes}
            selectedId={authState === 'authenticated' ? selectedId : null}
            onSelectNode={authState === 'authenticated' ? setSelectedId : null}
            selectable={authState === 'authenticated'}
          />
          {authState === 'authenticated' && (
            <div style={{
              position: 'absolute', top: 0, right: 0, bottom: 0,
              width: 300, zIndex: 400,
              display: 'flex', flexDirection: 'column',
              background: 'var(--bg-panel)',
              borderLeft: '1px solid var(--border)',
              boxShadow: '-4px 0 16px rgba(0,0,0,0.3)',
              overflow: 'hidden',
            }}>
              {selectedNode ? (
                <NodeDetail
                  node={selectedNode}
                  onClose={() => setSelectedId(null)}
                  onApprove={approveNode}
                  onReject={rejectNode}
                  onRemove={removeNode}
                  onConfigure={configureNode}
                  onSetPosition={setNodePosition}
                  isAdmin={isAdmin}
                />
              ) : (
                <NodeSidebar
                  nodes={nodes}
                  selectedId={selectedId}
                  onSelect={setSelectedId}
                  onApprove={approveNode}
                  onReject={rejectNode}
                  isAdmin={isAdmin}
                />
              )}
            </div>
          )}
        </div>
      )}

      {tab === 'detections' && <DetectionsTab isAdmin={isAdmin} />}
      {tab === 'users' && isAdmin && <UsersTab user={user} />}
    </div>
  )
}
