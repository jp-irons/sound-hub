import { useState, useEffect } from 'react'
import { apiFetch } from '../auth.js'

function fmtLatLon(v) {
  return v != null ? v.toFixed(6) : '—'
}

function fmtAlt(v) {
  return v != null ? `${v.toFixed(1)} m` : '—'
}

export default function SettingsTab() {
  const [origin, setOrigin] = useState(null)     // null until loaded; 'none' sentinel not used — see notConfigured
  const [notConfigured, setNotConfigured] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const [lat, setLat] = useState('')
  const [lon, setLon] = useState('')
  const [alt, setAlt] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveMessage, setSaveMessage] = useState(null)

  const [clearArmed, setClearArmed] = useState(false)
  const [clearing, setClearing] = useState(false)

  const loadOrigin = () => {
    setLoading(true)
    setError(null)
    setNotConfigured(false)
    apiFetch('/origin')
      .then(async res => {
        if (res.status === 404) {
          setNotConfigured(true)
          setOrigin(null)
          return
        }
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
        const data = await res.json()
        setOrigin(data)
        setLat(String(data.lat))
        setLon(String(data.lon))
        setAlt(String(data.altM))
      })
      .catch(err => setError(err.message ?? String(err)))
      .finally(() => setLoading(false))
  }

  useEffect(() => { loadOrigin() }, [])

  const sty = {
    root: {
      display: 'flex', flexDirection: 'column', height: '100%',
      padding: '16px 20px', gap: 16, boxSizing: 'border-box',
      overflow: 'auto',
    },
    section: {
      background: 'var(--surface1, #1e1e1e)',
      border: '1px solid var(--border, #333)',
      borderRadius: 8, padding: '12px 16px',
      maxWidth: 480,
    },
    label: { fontSize: 11, color: 'var(--text-muted, #888)', marginBottom: 6, fontWeight: 600, letterSpacing: '0.04em', textTransform: 'uppercase' },
    hint: { fontSize: 11, color: 'var(--text-muted, #888)', marginTop: 2 },
    row: { display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' },
    input: {
      background: 'var(--surface2, #2a2a2a)',
      border: '1px solid var(--border, #333)',
      borderRadius: 4, padding: '6px 8px',
      color: 'var(--text, #eee)', fontSize: 12,
      fontFamily: 'monospace',
      width: '100%', minWidth: 0, boxSizing: 'border-box',
    },
    grid: { display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: 8, marginBottom: 10 },
    fieldLabel: { display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 },
    fieldLabelText: { color: 'var(--text-secondary, #ccc)' },
  }

  const handleSave = async () => {
    setError(null)
    setSaveMessage(null)
    const parsedLat = parseFloat(lat)
    const parsedLon = parseFloat(lon)
    const parsedAlt = parseFloat(alt)
    if (Number.isNaN(parsedLat) || Number.isNaN(parsedLon) || Number.isNaN(parsedAlt)) {
      setError('Latitude, longitude, and altitude must all be valid numbers.')
      return
    }
    setSaving(true)
    try {
      const res = await apiFetch('/origin', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lat: parsedLat, lon: parsedLon, altM: parsedAlt }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail ?? `${res.status} ${res.statusText}`)
      }
      const data = await res.json()
      setOrigin(data)
      setNotConfigured(false)
      setSaveMessage('Origin saved.')
    } catch (err) {
      setError(err.message ?? String(err))
    } finally {
      setSaving(false)
    }
  }

  const handleClear = async () => {
    if (!clearArmed) {
      setClearArmed(true)
      return
    }
    setClearing(true)
    setError(null)
    setSaveMessage(null)
    try {
      const res = await apiFetch('/origin', { method: 'DELETE' })
      if (!res.ok && res.status !== 204) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail ?? `${res.status} ${res.statusText}`)
      }
      setOrigin(null)
      setNotConfigured(true)
      setLat(''); setLon(''); setAlt('')
      setSaveMessage('Origin cleared — lat/lon projection and time-of-day filtering are unavailable until a new origin is set.')
    } catch (err) {
      setError(err.message ?? String(err))
    } finally {
      setClearing(false)
      setClearArmed(false)
    }
  }

  return (
    <div style={sty.root}>
      <div style={sty.section}>
        <div style={sty.label}>Hub Origin</div>
        <div style={{ ...sty.hint, marginBottom: 10 }}>
          The geographic datum (0,0,0) that all node array positions are measured relative to.
          Set it directly here, or from a surveyed node's coordinates via that node's position panel.
        </div>

        {loading ? (
          <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>Loading…</div>
        ) : (
          <>
            <div style={{ fontSize: 12, marginBottom: 12 }}>
              {notConfigured ? (
                <span style={{ color: 'var(--text-muted)' }}>Not configured yet.</span>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                  <span>
                    Current: {fmtLatLon(origin?.lat)}, {fmtLatLon(origin?.lon)}, {fmtAlt(origin?.altM)}
                  </span>
                  <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>
                    {origin?.setFrom
                      ? `Derived from node ${origin.setFrom}`
                      : 'Set manually'}
                    {origin?.setAt && ` · ${new Date(origin.setAt).toLocaleString()}`}
                  </span>
                </div>
              )}
            </div>

            <div style={sty.grid}>
              <label style={sty.fieldLabel}>
                <span style={sty.fieldLabelText}>Latitude</span>
                <input type="number" step="any" value={lat} placeholder="-27.123456"
                  onChange={e => setLat(e.target.value)} style={sty.input} />
              </label>
              <label style={sty.fieldLabel}>
                <span style={sty.fieldLabelText}>Longitude</span>
                <input type="number" step="any" value={lon} placeholder="153.123456"
                  onChange={e => setLon(e.target.value)} style={sty.input} />
              </label>
              <label style={sty.fieldLabel}>
                <span style={sty.fieldLabelText}>Alt (m)</span>
                <input type="number" step="any" value={alt} placeholder="42.0"
                  onChange={e => setAlt(e.target.value)} style={sty.input} />
              </label>
            </div>

            <div style={sty.row}>
              <button
                type="button"
                className="btn btn-primary"
                style={{ fontSize: 12, padding: '6px 14px' }}
                disabled={saving}
                onClick={handleSave}
              >
                {saving ? 'Saving…' : 'Save origin'}
              </button>

              {!notConfigured && (
                <button
                  type="button"
                  className="btn"
                  style={{
                    fontSize: 12, padding: '6px 14px',
                    color: clearArmed ? 'var(--red, #f44336)' : undefined,
                    borderColor: clearArmed ? 'var(--red, #f44336)' : undefined,
                  }}
                  disabled={clearing}
                  onClick={handleClear}
                  onBlur={() => setClearArmed(false)}
                >
                  {clearing ? 'Clearing…' : clearArmed ? 'Click again to confirm' : 'Clear origin'}
                </button>
              )}
            </div>

            {saveMessage && (
              <div style={{ fontSize: 12, color: 'var(--green, #4caf50)', marginTop: 10 }}>
                {saveMessage}
              </div>
            )}
            {error && (
              <div style={{
                fontSize: 12, color: 'var(--red, #f44336)',
                background: 'rgba(244,67,54,0.12)', borderRadius: 4, padding: '6px 8px',
                marginTop: 10,
              }}>
                {error}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
