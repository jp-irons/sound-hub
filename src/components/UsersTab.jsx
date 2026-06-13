import { useState, useEffect, useCallback } from 'react'
import { apiFetch, AuthError } from '../auth.js'

const MIN_PASSWORD_LENGTH = 8

export default function UsersTab({ user: currentUser }) {
  const [users, setUsers]       = useState([])
  const [error, setError]       = useState(null)
  const [busy,  setBusy]        = useState(false)

  // Add-user form state
  const [addUsername, setAddUsername] = useState('')
  const [addRole,     setAddRole]     = useState('viewer')
  const [addPassword, setAddPassword] = useState('')
  const [addConfirm,  setAddConfirm]  = useState('')
  const [addError,    setAddError]    = useState(null)
  const [addBusy,     setAddBusy]     = useState(false)

  // Change-password inline state: { [username]: { password, confirm, error, busy } }
  const [pwForms, setPwForms] = useState({})

  // Delete confirm state: username currently pending confirm, or null
  const [deleteConfirm, setDeleteConfirm] = useState(null)

  const loadUsers = useCallback(async () => {
    try {
      const res = await apiFetch('/users')
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
      setUsers(await res.json())
      setError(null)
    } catch (err) {
      if (err instanceof AuthError) return
      setError(err.message ?? String(err))
    }
  }, [])

  useEffect(() => { loadUsers() }, [loadUsers])

  // -------------------------------------------------------------------------
  // Add user
  // -------------------------------------------------------------------------
  async function handleAdd(e) {
    e.preventDefault()
    setAddError(null)
    if (!addUsername.trim())            { setAddError('Username is required.'); return }
    if (addPassword.length < MIN_PASSWORD_LENGTH)
      { setAddError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`); return }
    if (addPassword !== addConfirm)     { setAddError('Passwords do not match.'); return }

    setAddBusy(true)
    try {
      const res = await apiFetch('/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: addUsername.trim(), password: addPassword, role: addRole }),
      })
      const body = await res.json().catch(() => ({}))
      if (!res.ok) { setAddError(body?.detail ?? `Error ${res.status}`); return }
      setAddUsername(''); setAddRole('viewer'); setAddPassword(''); setAddConfirm('')
      await loadUsers()
    } catch (err) {
      if (err instanceof AuthError) return
      setAddError(err.message ?? String(err))
    } finally {
      setAddBusy(false)
    }
  }

  // -------------------------------------------------------------------------
  // Delete user
  // -------------------------------------------------------------------------
  async function handleDelete(username) {
    setBusy(true)
    try {
      const res = await apiFetch(`/users/${encodeURIComponent(username)}`, { method: 'DELETE' })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        setError(body?.detail ?? `Error ${res.status}`)
      } else {
        setDeleteConfirm(null)
        await loadUsers()
      }
    } catch (err) {
      if (err instanceof AuthError) return
      setError(err.message ?? String(err))
    } finally {
      setBusy(false)
    }
  }

  // -------------------------------------------------------------------------
  // Change password
  // -------------------------------------------------------------------------
  function openPwForm(username) {
    setPwForms(f => ({ ...f, [username]: { password: '', confirm: '', error: null, busy: false } }))
  }
  function closePwForm(username) {
    setPwForms(f => { const n = { ...f }; delete n[username]; return n })
  }
  function setPwField(username, field, value) {
    setPwForms(f => ({ ...f, [username]: { ...f[username], [field]: value } }))
  }

  async function handleChangePassword(username) {
    const form = pwForms[username]
    if (!form) return
    if (form.password.length < MIN_PASSWORD_LENGTH) {
      setPwForms(f => ({ ...f, [username]: { ...f[username], error: `At least ${MIN_PASSWORD_LENGTH} characters.` } }))
      return
    }
    if (form.password !== form.confirm) {
      setPwForms(f => ({ ...f, [username]: { ...f[username], error: 'Passwords do not match.' } }))
      return
    }
    setPwForms(f => ({ ...f, [username]: { ...f[username], busy: true, error: null } }))
    try {
      const res = await apiFetch(`/users/${encodeURIComponent(username)}/password`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: form.password }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        setPwForms(f => ({ ...f, [username]: { ...f[username], busy: false, error: body?.detail ?? `Error ${res.status}` } }))
      } else {
        closePwForm(username)
      }
    } catch (err) {
      if (err instanceof AuthError) return
      setPwForms(f => ({ ...f, [username]: { ...f[username], busy: false, error: err.message ?? String(err) } }))
    }
  }

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------
  return (
    <div style={{ flex: 1, overflow: 'auto', padding: 24 }}>
      <h2 style={{ ...s.heading, marginBottom: 20 }}>Users</h2>

      {error && <div style={s.errorBanner}>{error}</div>}

      {/* User table */}
      <table style={s.table}>
        <thead>
          <tr>
            <th style={s.th}>Username</th>
            <th style={s.th}>Role</th>
            <th style={s.th}>Created</th>
            <th style={{ ...s.th, textAlign: 'right' }}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {users.map(u => (
            <>
              <tr key={u.username} style={s.tr}>
                <td style={s.td}>
                  <span style={{ fontWeight: u.username === currentUser?.username ? 600 : 400 }}>
                    {u.username}
                  </span>
                  {u.username === currentUser?.username && (
                    <span style={s.youBadge}>you</span>
                  )}
                </td>
                <td style={s.td}>
                  <span style={{ ...s.roleBadge, ...(u.role === 'admin' ? s.adminBadge : s.viewerBadge) }}>
                    {u.role}
                  </span>
                </td>
                <td style={{ ...s.td, color: 'var(--text-muted, #888)', fontSize: 11 }}>
                  {u.created_at ? new Date(u.created_at).toLocaleDateString() : '—'}
                </td>
                <td style={{ ...s.td, textAlign: 'right' }}>
                  {!pwForms[u.username] && (
                    <button style={s.actionBtn} onClick={() => openPwForm(u.username)}>
                      Change password
                    </button>
                  )}
                  {deleteConfirm === u.username ? (
                    <>
                      <span style={{ fontSize: 11, color: 'var(--text-muted)', marginRight: 6 }}>
                        Sure?
                      </span>
                      <button
                        style={{ ...s.actionBtn, ...s.dangerBtn }}
                        onClick={() => handleDelete(u.username)}
                        disabled={busy}
                      >
                        Yes, delete
                      </button>
                      <button style={s.actionBtn} onClick={() => setDeleteConfirm(null)}>
                        Cancel
                      </button>
                    </>
                  ) : (
                    <button
                      style={{ ...s.actionBtn, ...s.dangerBtn }}
                      onClick={() => setDeleteConfirm(u.username)}
                    >
                      Delete
                    </button>
                  )}
                </td>
              </tr>

              {/* Inline change-password row */}
              {pwForms[u.username] && (
                <tr key={`${u.username}-pw`} style={{ background: 'var(--surface2, #2a2a2a)' }}>
                  <td colSpan={4} style={{ padding: '10px 12px' }}>
                    <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start', flexWrap: 'wrap' }}>
                      <input
                        style={{ ...s.input, width: 160 }}
                        type="password"
                        placeholder="New password"
                        value={pwForms[u.username].password}
                        onChange={e => setPwField(u.username, 'password', e.target.value)}
                        autoFocus
                      />
                      <input
                        style={{ ...s.input, width: 160 }}
                        type="password"
                        placeholder="Confirm"
                        value={pwForms[u.username].confirm}
                        onChange={e => setPwField(u.username, 'confirm', e.target.value)}
                      />
                      <button
                        style={{ ...s.actionBtn, background: 'var(--accent, #4da6ff)', color: '#fff', border: 'none' }}
                        onClick={() => handleChangePassword(u.username)}
                        disabled={pwForms[u.username].busy}
                      >
                        {pwForms[u.username].busy ? 'Saving…' : 'Save'}
                      </button>
                      <button style={s.actionBtn} onClick={() => closePwForm(u.username)}>
                        Cancel
                      </button>
                      {pwForms[u.username].error && (
                        <span style={{ fontSize: 11, color: 'var(--red, #e05555)', alignSelf: 'center' }}>
                          {pwForms[u.username].error}
                        </span>
                      )}
                    </div>
                  </td>
                </tr>
              )}
            </>
          ))}

          {users.length === 0 && (
            <tr>
              <td colSpan={4} style={{ ...s.td, color: 'var(--text-muted, #888)', textAlign: 'center', padding: 24 }}>
                No users found.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {/* Add user form */}
      <div style={s.addSection}>
        <h3 style={{ ...s.heading, fontSize: 13, marginBottom: 14 }}>Add user</h3>
        <form onSubmit={handleAdd} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            <label style={s.label}>Username</label>
            <input
              style={{ ...s.input, width: 150 }}
              type="text"
              value={addUsername}
              onChange={e => setAddUsername(e.target.value)}
              disabled={addBusy}
              autoComplete="off"
            />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            <label style={s.label}>Role</label>
            <select
              style={{ ...s.input, width: 100 }}
              value={addRole}
              onChange={e => setAddRole(e.target.value)}
              disabled={addBusy}
            >
              <option value="viewer">viewer</option>
              <option value="admin">admin</option>
            </select>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            <label style={s.label}>Password</label>
            <input
              style={{ ...s.input, width: 160 }}
              type="password"
              value={addPassword}
              onChange={e => setAddPassword(e.target.value)}
              disabled={addBusy}
              autoComplete="new-password"
            />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            <label style={s.label}>Confirm</label>
            <input
              style={{ ...s.input, width: 160 }}
              type="password"
              value={addConfirm}
              onChange={e => setAddConfirm(e.target.value)}
              disabled={addBusy}
              autoComplete="new-password"
            />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            <label style={{ ...s.label, visibility: 'hidden' }}>_</label>
            <button type="submit" style={{ ...s.actionBtn, background: 'var(--accent, #4da6ff)', color: '#fff', border: 'none', padding: '7px 16px' }} disabled={addBusy}>
              {addBusy ? 'Adding…' : 'Add user'}
            </button>
          </div>
          {addError && (
            <div style={{ ...s.errorBanner, alignSelf: 'flex-end', marginBottom: 0 }}>{addError}</div>
          )}
        </form>
      </div>
    </div>
  )
}

const s = {
  heading: {
    fontSize: 15, fontWeight: 600,
    color: 'var(--text-primary, #e8e8e8)',
    margin: 0,
  },
  errorBanner: {
    fontSize: 12, color: 'var(--red, #e05555)',
    background: 'rgba(219,68,55,0.1)',
    border: '1px solid rgba(219,68,55,0.25)',
    borderRadius: 5, padding: '6px 10px',
    marginBottom: 14,
  },
  table: {
    width: '100%', borderCollapse: 'collapse',
    fontSize: 12,
    marginBottom: 28,
  },
  th: {
    textAlign: 'left', padding: '6px 12px',
    fontSize: 11, fontWeight: 600, letterSpacing: '0.05em', textTransform: 'uppercase',
    color: 'var(--text-muted, #888)',
    borderBottom: '1px solid var(--border, #333)',
  },
  tr: {
    borderBottom: '1px solid var(--border, #2a2a2a)',
  },
  td: {
    padding: '9px 12px',
    color: 'var(--text-primary, #e8e8e8)',
    verticalAlign: 'middle',
  },
  youBadge: {
    fontSize: 10, fontWeight: 600, letterSpacing: '0.04em',
    color: 'var(--accent, #4da6ff)',
    background: 'rgba(77,166,255,0.12)',
    border: '1px solid rgba(77,166,255,0.25)',
    borderRadius: 3, padding: '1px 5px',
    marginLeft: 7,
  },
  roleBadge: {
    fontSize: 10, fontWeight: 600, letterSpacing: '0.04em',
    borderRadius: 3, padding: '2px 6px',
  },
  adminBadge: {
    color: 'var(--accent, #4da6ff)',
    background: 'rgba(77,166,255,0.1)',
    border: '1px solid rgba(77,166,255,0.2)',
  },
  viewerBadge: {
    color: 'var(--text-muted, #888)',
    background: 'var(--surface2, #2a2a2a)',
    border: '1px solid var(--border, #333)',
  },
  actionBtn: {
    fontSize: 11, color: 'var(--text-secondary, #aaa)',
    background: 'transparent',
    border: '1px solid var(--border, #333)',
    borderRadius: 4, padding: '4px 10px', cursor: 'pointer',
    marginLeft: 4,
  },
  dangerBtn: {
    color: 'var(--red, #e05555)',
    borderColor: 'rgba(219,68,55,0.3)',
  },
  addSection: {
    background: 'var(--surface1, #1e1e1e)',
    border: '1px solid var(--border, #333)',
    borderRadius: 8, padding: '18px 20px',
    maxWidth: 800,
  },
  label: {
    fontSize: 11, fontWeight: 500,
    color: 'var(--text-muted, #888)',
  },
  input: {
    background: 'var(--surface2, #2a2a2a)',
    border: '1px solid var(--border, #333)',
    borderRadius: 5, padding: '6px 8px',
    fontSize: 12, color: 'var(--text-primary, #e8e8e8)',
    outline: 'none',
  },
}
