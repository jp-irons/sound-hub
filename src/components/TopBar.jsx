import { useState, useEffect } from 'react'
import { useIsMobile } from '../hooks/useBreakpoint.js'

function relativeTime(isoString) {
  if (!isoString) return '—'
  const diff = Date.now() - new Date(isoString).getTime()
  if (diff < 60000) return `${Math.floor(diff / 1000)}s ago`
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`
  return `${Math.floor(diff / 3600000)}h ago`
}

export default function TopBar({ totalNodes, onlineCount, nodes, user, onLogout, onSignIn }) {
  const [tick, setTick] = useState(0)
  const isMobile = useIsMobile()
  useEffect(() => {
    const t = setInterval(() => setTick(n => n + 1), 1000)
    return () => clearInterval(t)
  }, [])

  const lastTrigger = nodes
    .flatMap(n => n.audio?.lastTriggerAt ? [new Date(n.audio.lastTriggerAt)] : [])
    .sort((a, b) => b - a)[0]

  const degraded = nodes.filter(n => n.status === 'degraded').length
  const offline  = nodes.filter(n => n.status === 'offline').length

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 20,
      padding: '0 16px', height: 44,
      background: 'var(--bg-panel)', borderBottom: '1px solid var(--border)',
      flexShrink: 0,
    }}>
      {/* Title */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ fontSize: 16 }}>🎙</span>
        <span style={{ fontWeight: 700, fontSize: 14, letterSpacing: '-0.01em' }}>
          Sound Hub
        </span>
        <span style={{
          fontSize: 10, fontWeight: 600, letterSpacing: '0.06em',
          color: 'var(--text-muted)', textTransform: 'uppercase',
          marginLeft: 2,
        }}>
          v0.1
        </span>
      </div>

      {!isMobile && <div style={{ width: 1, height: 20, background: 'var(--border)' }} />}

      {/* Node status summary — hidden on mobile */}
      {!isMobile && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <StatusChip count={onlineCount} label="healthy" color="var(--green)" />
          {degraded > 0 && <StatusChip count={degraded} label="degraded" color="var(--yellow)" />}
          {offline  > 0 && <StatusChip count={offline}  label="offline"  color="var(--red)" />}
          <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>
            {totalNodes} nodes
          </span>
        </div>
      )}

      <div style={{ flex: 1 }} />

      {/* Last trigger — hidden on mobile */}
      {!isMobile && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--text-secondary)', fontSize: 12 }}>
          <span>Last trigger:</span>
          <span style={{ color: lastTrigger ? 'var(--text-primary)' : 'var(--text-muted)' }}>
            {lastTrigger ? relativeTime(lastTrigger.toISOString()) : 'none'}
          </span>
        </div>
      )}

      {!isMobile && <div style={{ width: 1, height: 20, background: 'var(--border)' }} />}

      {/* User identity + sign out */}
      {user ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <div style={{
              width: 22, height: 22, borderRadius: '50%',
              background: 'var(--accent, #4da6ff)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: 11, fontWeight: 700, color: '#fff',
            }}>
              {user.username[0].toUpperCase()}
            </div>
            <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
              {user.username}
            </span>
            <span style={{
              fontSize: 10, color: 'var(--text-muted)',
              background: 'var(--surface2, #2a2a2a)',
              border: '1px solid var(--border, #333)',
              borderRadius: 3, padding: '1px 5px',
            }}>
              {user.role}
            </span>
          </div>
          <button
            onClick={onLogout}
            style={{
              fontSize: 11, color: 'var(--text-muted)',
              background: 'transparent', border: '1px solid var(--border, #333)',
              borderRadius: 4, padding: '2px 8px', cursor: 'pointer',
            }}
          >
            Sign out
          </button>
        </div>
      ) : (
        <button
          onClick={onSignIn}
          style={{
            display: 'flex', alignItems: 'center', gap: 6,
            background: 'transparent',
            border: '1px solid var(--border, #333)',
            borderRadius: 4, padding: '3px 10px',
            cursor: 'pointer',
          }}
        >
          <div className="status-dot online" />
          <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>Sign in</span>
        </button>
      )}
    </div>
  )
}

function StatusChip({ count, label, color }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
      <div style={{
        width: 7, height: 7, borderRadius: '50%', background: color,
        boxShadow: label === 'healthy' ? `0 0 6px ${color}` : 'none',
      }} />
      <span style={{ fontSize: 12, color }}>
        {count} {label}
      </span>
    </div>
  )
}
