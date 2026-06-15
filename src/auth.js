/**
 * Auth module — token store and auth-aware fetch wrapper.
 *
 * Token is persisted to localStorage so it survives page reloads.
 * On startup the stored token is restored; App.jsx validates it
 * against /auth/me before trusting it.
 */

const API_BASE = '/api'
const TOKEN_KEY = 'sound_hub_token'

let _token = localStorage.getItem(TOKEN_KEY)
let _onUnauthenticated = null   // callback set by App to trigger re-login

export function getToken()         { return _token }
export function setToken(t)        { _token = t; localStorage.setItem(TOKEN_KEY, t) }
export function clearToken()       { _token = null; localStorage.removeItem(TOKEN_KEY) }

/** Register a callback App.jsx calls so auth errors flip it back to login. */
export function onUnauthenticated(cb) { _onUnauthenticated = cb }

/**
 * Auth error — distinguished from network/server errors so callers can
 * decide whether to show the auth overlay instead of an error banner.
 */
export class AuthError extends Error {
  constructor(msg = 'Not authenticated') {
    super(msg)
    this.name = 'AuthError'
  }
}

/**
 * Drop-in fetch wrapper.  Injects Bearer token when present; on 401
 * clears the token, fires the unauthenticated callback, and throws AuthError.
 *
 * Usage: same as fetch() — apiFetch('/nodes') or apiFetch('/nodes', { method: 'POST', ... })
 */
export async function apiFetch(path, options = {}) {
  const headers = { ...(options.headers ?? {}) }
  if (_token) headers['Authorization'] = `Bearer ${_token}`

  const res = await fetch(`${API_BASE}${path}`, { ...options, headers })

  if (res.status === 401) {
    clearToken()
    _onUnauthenticated?.()
    throw new AuthError()
  }

  return res
}
