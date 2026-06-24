import { useState, useEffect, useCallback } from 'react'
import TopBar from './components/TopBar.jsx'
import NodeSidebar from './components/NodeSidebar.jsx'
import MapView from './components/MapView.jsx'
import NodeDetail from './components/NodeDetail.jsx'
import DetectionsTab from './components/DetectionsTab.jsx'
import ToolsTab from './components/ToolsTab.jsx'
import AuthOverlay from './components/AuthOverlay.jsx'
import UsersTab from './components/UsersTab.jsx'
import SettingsTab from './components/SettingsTab.jsx'
import { apiFetch, getToken, setToken, clearToken, onUnauthenticated, AuthError } from './auth.js'
import { useIsMobile } from './hooks/useBreakpoint.js'

const API_BASE = '/api'
const POLL_INTERVAL_MS = 5000

const TAB_KEY = 'app.activeTab'
const TABS = ['map', 'detections', 'tools', 'users', 'settings']

function loadTab() {
  try {
    const raw = localStorage.getItem(TAB_KEY)
    return TABS.includes(raw) ? raw : 'map'
  } catch { return 'map' }
}

export default function App() {
  // 'login' | 'setup' | 'unauthenticated' | 'authenticated'
  const [authState, setAuthState] = useState('unauthenticated')
  const [user, setUser] = useState(null)   // { username, role }

  // Persisted so a reload (deliberate or forced by a flaky connection, e.g.
  // the van) lands back on the tab you were viewing instead of always
  // resetting to the map.
  const [tab, setTab] = useState(loadTab)
  const [nodes, setNodes] = useState([])             // full node list — authenticated only
  const [publicNodes, setPublicNodes] = useState([]) // slim node list — unauthenticated
  const [selectedId, setSelectedId] = useState(null)
  const [error, setError] = useState(null)

  const isMobile = useIsMobile()
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false)

  const isAdmin = user?.role === 'admin'

  useEffect(() => {
    try { localStorage.setItem(TAB_KEY, tab) } catch { /* ignore */ }
  }, [tab])

  // Guard against a restored admin-only tab (tools/users) when the
  // logged-in user isn't an admin — e.g. a different account on the same
  // browser, or a role change since the tab was last saved.
  useEffect(() => {
    if ((tab === 'tools' || tab === 'users' || tab === 'settings') && authState === 'authenticated' && !isAdmin) {
      setTab('map')
    }
  }, [tab, authState, isAdmin])

  // Wire up the 401 callback so any apiFetch can flip us back to login.
  useEffect(() => {
    onUnauthenticated(() => {
      setUser(null)
      setAuthState('login')
    })
  }, [])

  // Re-validate the stored token against /auth/me. Used both on first mount
  // and again whenever the tab regains visibility (see effect below) — iOS
  // can throttle/abort an in-flight fetch while a tab is backgrounded, or
  // evict and reload the tab outright, either of which can otherwise leave
  // authState stranded at its initial value even though a valid token is
  // sitting in localStorage. A failed check here deliberately does NOT log
  // the user out — it just leaves the current state alone so the next
  // visibility/pageshow event gets another chance to confirm it.
  const checkAuth = useCallback(async () => {
    if (!getToken()) return false
    try {
      const res = await apiFetch('/auth/me')
      if (res.ok) {
        const body = await res.json()
        setUser({ username: body.username, role: body.role })
        setAuthState('authenticated')
        return true
      }
    } catch {
      // Backend unreachable / request aborted — leave state untouched, retry later
    }
    return false
  }, [])

  // On mount: if a stored token exists, validate it and restore the session
  // silently. If that fails (expired / invalid / unreachable), fall through
  // to the normal setup/login flow.
  useEffect(() => {
    async function init() {
      const ok = await checkAuth()
      if (ok) return
      // No stored token (or it failed) — check for first-run setup
      try {
        const res = await fetch(`${API_BASE}/auth/status`)
        const body = await res.json()
        if (body.setup_required) setAuthState('setup')
        // else leave as 'unauthenticated' — user browses map and signs in on demand
      } catch {
        // Backend unreachable — leave as 'unauthenticated', error will surface on submit.
      }
    }
    init()
  }, [checkAuth])

  // Re-check auth whenever the tab becomes visible again (app-switch resume,
  // bfcache restore via pageshow). This is what recovers the header if the
  // initial /auth/me call above got dropped while the tab was backgrounded.
  useEffect(() => {
    function handleVisibility() {
      if (document.visibilityState === 'visible' && getToken() && authState !== 'authenticated') {
        checkAuth()
      }
    }
    function handlePageShow() {
      if (getToken() && authState !== 'authenticated') checkAuth()
    }
    document.addEventListener('visibilitychange', handleVisibility)
    window.addEventListener('pageshow', handlePageShow)
    window.addEventListener('focus', handlePageShow)
    return () => {
      document.removeEventListener('visibilitychange', handleVisibility)
      window.removeEventListener('pageshow', handlePageShow)
      window.removeEventListener('focus', handlePageShow)
    }
  }, [checkAuth, authState])

  // Safety net #1: visibilitychange/pageshow/focus are not reliable everywhere
  // — mobile Safari in particular can leave document.visibilityState stuck on
  // 'hidden' after a tab is back in the foreground (observed directly:
  // hasFocus() true, visibilityState still 'hidden'). When that happens, the
  // browser also throttles/pauses JS timers for documents it still considers
  // hidden, so even this retry can go silent. If a token is sitting in
  // storage but we haven't confirmed it yet, keep retrying on a short
  // interval until checkAuth succeeds (or the token goes away) instead of
  // waiting on an event that may never come.
  useEffect(() => {
    if (authState === 'authenticated') return
    if (!getToken()) return
    const id = setInterval(checkAuth, 3000)
    return () => clearInterval(id)
  }, [authState, checkAuth])

  // Safety net #2: a real user gesture resumes a throttled/paused JS context
  // even when the Page Visibility API is reporting the wrong thing, so a tap
  // or click on the (apparently logged-out) page is also a trigger to retry.
  // Cheap — it's a no-op once authState is 'authenticated'.
  useEffect(() => {
    if (authState === 'authenticated') return
    if (!getToken()) return
    function handleInteraction() { checkAuth() }
    document.addEventListener('touchstart', handleInteraction, { passive: true })
    document.addEventListener('click', handleInteraction)
    return () => {
      document.removeEventListener('touchstart', handleInteraction)
      document.removeEventListener('click', handleInteraction)
    }
  }, [authState, checkAuth])

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
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>

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
          <button style={tabStyle('tools')} onClick={() => setTab('tools')}>Tools</button>
        )}
        {isAdmin && (
          <button style={tabStyle('users')} onClick={() => setTab('users')}>Users</button>
        )}
        {isAdmin && (
          <button style={tabStyle('settings')} onClick={() => setTab('settings')}>Settings</button>
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
            onSelectNode={authState === 'authenticated'
              ? (id) => { setSelectedId(id); if (isMobile) setMobileSidebarOpen(true) }
              : null}
            selectable={authState === 'authenticated'}
          />

          {/* Mobile: floating Nodes button */}
          {authState === 'authenticated' && isMobile && !mobileSidebarOpen && (
            <button
              onClick={() => setMobileSidebarOpen(true)}
              style={{
                position: 'absolute', bottom: 20, right: 16, zIndex: 500,
                display: 'flex', alignItems: 'center', gap: 7,
                background: 'var(--bg-panel)',
                border: '1px solid var(--border)',
                borderRadius: 20,
                padding: '8px 14px',
                color: 'var(--text-primary)',
                fontSize: 13, fontWeight: 600,
                boxShadow: '0 4px 16px rgba(0,0,0,0.4)',
                cursor: 'pointer',
              }}
            >
              <span style={{ fontSize: 15 }}>☰</span>
              Nodes
              {nodes.filter(n => n.approvalStatus === 'pending').length > 0 && (
                <span style={{
                  background: 'var(--yellow)',
                  color: '#000',
                  borderRadius: 8,
                  fontSize: 10, fontWeight: 700,
                  padding: '1px 5px',
                }}>
                  {nodes.filter(n => n.approvalStatus === 'pending').length}
                </span>
              )}
            </button>
          )}

          {authState === 'authenticated' && (
            <div style={isMobile ? {
              // Mobile: full-screen overlay, hidden when closed
              position: 'fixed', inset: 0, zIndex: 400,
              display: mobileSidebarOpen ? 'flex' : 'none',
              flexDirection: 'column',
              background: 'var(--bg-panel)',
            } : {
              // Desktop: right-side panel, always visible
              position: 'absolute', top: 0, right: 0, bottom: 0,
              width: 300, zIndex: 400,
              display: 'flex', flexDirection: 'column',
              background: 'var(--bg-panel)',
              borderLeft: '1px solid var(--border)',
              boxShadow: '-4px 0 16px rgba(0,0,0,0.3)',
              overflow: 'hidden',
            }}>

              {/* Mobile back-to-map header */}
              {isMobile && (
                <div style={{
                  display: 'flex', alignItems: 'center',
                  padding: '10px 14px',
                  borderBottom: '1px solid var(--border)',
                  flexShrink: 0,
                }}>
                  <button
                    onClick={() => { setMobileSidebarOpen(false); setSelectedId(null) }}
                    style={{
                      background: 'none', border: 'none',
                      color: 'var(--blue)', fontSize: 14,
                      cursor: 'pointer', padding: '4px 0',
                      display: 'flex', alignItems: 'center', gap: 5,
                    }}
                  >
                    ← Map
                  </button>
                </div>
              )}

              {selectedNode ? (
                <NodeDetail
                  node={selectedNode}
                  onClose={() => { setSelectedId(null); if (isMobile) setMobileSidebarOpen(false) }}
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
                  onSelect={(id) => { setSelectedId(id) }}
                  onApprove={approveNode}
                  onReject={rejectNode}
                  isAdmin={isAdmin}
                />
              )}
            </div>
          )}
        </div>
      )}

      {tab === 'detections' && <DetectionsTab />}
      {tab === 'tools' && isAdmin && <ToolsTab />}
      {tab === 'users' && isAdmin && <UsersTab user={user} />}
      {tab === 'settings' && isAdmin && <SettingsTab />}
    </div>
  )
}
