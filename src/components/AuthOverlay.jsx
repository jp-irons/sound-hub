import { useState } from 'react'
import { useIsMobile } from '../hooks/useBreakpoint.js'

const API_BASE = '/api'

const MIN_PASSWORD_LENGTH = 8

/**
 * Full-screen auth overlay.
 *
 * mode='setup'  — first run, no users exist yet
 * mode='login'  — returning user; shows "Browse without signing in" option
 *
 * onSuccess(token, username, role) — called on successful auth
 * onBrowse() — called when user chooses to browse unauthenticated (login mode only)
 */
export default function AuthOverlay({ mode, onSuccess, onBrowse }) {
  const [username, setUsername]   = useState('')
  const [password, setPassword]   = useState('')
  const [confirm,  setConfirm]    = useState('')
  const [error,    setError]      = useState(null)
  const [busy,     setBusy]       = useState(false)

  const isMobile = useIsMobile()
  const isSetup = mode === 'setup'

  function validate() {
    if (!username.trim()) return 'Username is required.'
    if (password.length < MIN_PASSWORD_LENGTH)
      return `Password must be at least ${MIN_PASSWORD_LENGTH} characters.`
    if (isSetup && password !== confirm)
      return 'Passwords do not match.'
    return null
  }

  async function handleSubmit(e) {
    e.preventDefault()
    setError(null)

    const validationError = validate()
    if (validationError) { setError(validationError); return }

    setBusy(true)
    try {
      const endpoint = isSetup ? '/auth/setup' : '/auth/login'
      const res = await fetch(`${API_BASE}${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username.trim(), password }),
      })

      const body = await res.json().catch(() => ({}))

      if (!res.ok) {
        setError(body?.detail ?? `Error ${res.status} — please try again.`)
        return
      }

      if (isSetup) {
        // After setup succeeds, immediately log in to get a token
        const loginRes = await fetch(`${API_BASE}/auth/login`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: username.trim(), password }),
        })
        const loginBody = await loginRes.json().catch(() => ({}))
        if (!loginRes.ok) {
          setError('Account created but login failed — please refresh and sign in.')
          return
        }
        onSuccess(loginBody.access_token, username.trim(), loginBody.role)
      } else {
        onSuccess(body.access_token, username.trim(), body.role)
      }
    } catch (err) {
      setError('Could not reach the server — is the backend running?')
    } finally {
      setBusy(false)
    }
  }

  const cardStyle = {
    ...styles.card,
    width: isMobile ? 'calc(100vw - 32px)' : 360,
    padding: isMobile ? '24px 20px' : '32px 36px',
    maxHeight: 'calc(100dvh - 32px)',
    overflowY: 'auto',
  }

  return (
    <div style={styles.backdrop}>
      <div style={cardStyle}>
        {/* Header */}
        <div style={styles.header}>
          <span style={{ fontSize: 22 }}>🎙</span>
          <span style={styles.title}>Sound Hub</span>
        </div>

        <p style={styles.subtitle}>
          {isSetup
            ? 'Create your admin account to get started.'
            : 'Sign in to continue.'}
        </p>

        {isSetup && (
          <div style={styles.notice}>
            First run — no accounts exist yet.
          </div>
        )}

        <form onSubmit={handleSubmit} style={styles.form}>
          <label style={styles.label}>Username</label>
          <input
            style={styles.input}
            type="text"
            autoComplete="username"
            autoFocus
            value={username}
            onChange={e => setUsername(e.target.value)}
            disabled={busy}
          />

          <label style={styles.label}>Password</label>
          <input
            style={styles.input}
            type="password"
            autoComplete={isSetup ? 'new-password' : 'current-password'}
            value={password}
            onChange={e => setPassword(e.target.value)}
            disabled={busy}
          />

          {isSetup && (
            <>
              <label style={styles.label}>Confirm password</label>
              <input
                style={styles.input}
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={e => setConfirm(e.target.value)}
                disabled={busy}
              />
            </>
          )}

          {error && <div style={styles.error}>{error}</div>}

          <button type="submit" style={styles.button} disabled={busy}>
            {busy ? 'Please wait…' : isSetup ? 'Create account' : 'Sign in'}
          </button>
        </form>

        {/* Cancel — returns to unauthenticated map view (login mode only) */}
        {!isSetup && onBrowse && (
          <button
            onClick={() => {
              // Unmounting this overlay (and its autoFocus'd username input)
              // while the keyboard is still open is a known iOS Safari edge
              // case that leaves the visual viewport stuck in its
              // "keyboard open" offset — scrollTo() can't undo it afterwards
              // because there's nothing to actually scroll. Blur first and
              // give the keyboard a moment to dismiss cleanly before the
              // overlay (and the focused input) actually goes away.
              document.activeElement?.blur()
              setTimeout(onBrowse, 50)
            }}
            style={styles.browseLink}
          >
            Cancel
          </button>
        )}
      </div>
    </div>
  )
}

const styles = {
  backdrop: {
    position: 'fixed', inset: 0, zIndex: 1000,
    background: 'rgba(0,0,0,0.72)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
  },
  card: {
    background: 'var(--surface1, #1e1e1e)',
    border: '1px solid var(--border, #333)',
    borderRadius: 10,
    padding: '32px 36px',
    width: 360,
    boxShadow: '0 8px 40px rgba(0,0,0,0.6)',
  },
  header: {
    display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6,
  },
  title: {
    fontWeight: 700, fontSize: 20,
    color: 'var(--text-primary, #e8e8e8)',
  },
  subtitle: {
    fontSize: 13, color: 'var(--text-muted, #888)',
    margin: '0 0 20px',
  },
  notice: {
    fontSize: 12, color: 'var(--accent, #4da6ff)',
    background: 'rgba(77,166,255,0.08)',
    border: '1px solid rgba(77,166,255,0.2)',
    borderRadius: 5, padding: '6px 10px',
    marginBottom: 18,
  },
  form: {
    display: 'flex', flexDirection: 'column', gap: 6,
  },
  label: {
    fontSize: 12, fontWeight: 500,
    color: 'var(--text-secondary, #aaa)',
    marginTop: 8,
  },
  input: {
    background: 'var(--surface2, #2a2a2a)',
    border: '1px solid var(--border, #333)',
    borderRadius: 5, padding: '8px 10px',
    fontSize: 13, color: 'var(--text-primary, #e8e8e8)',
    outline: 'none',
    width: '100%', boxSizing: 'border-box',
  },
  error: {
    fontSize: 12, color: 'var(--red, #e05555)',
    background: 'rgba(219,68,55,0.1)',
    border: '1px solid rgba(219,68,55,0.25)',
    borderRadius: 5, padding: '6px 10px',
    marginTop: 8,
  },
  button: {
    marginTop: 20,
    background: 'var(--accent, #4da6ff)',
    color: '#fff', fontWeight: 600, fontSize: 13,
    border: 'none', borderRadius: 6,
    padding: '10px 0', cursor: 'pointer',
    opacity: 1, transition: 'opacity 0.15s',
  },
  browseLink: {
    marginTop: 16,
    background: 'none', border: 'none',
    color: 'var(--text-muted, #888)',
    fontSize: 12, cursor: 'pointer',
    textDecoration: 'underline',
    padding: 0,
    display: 'block', width: '100%', textAlign: 'center',
  },
}
