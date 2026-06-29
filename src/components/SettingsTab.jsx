import { useState, useEffect } from 'react'
import { apiFetch } from '../auth.js'

function fmtLatLon(v) {
  return v != null ? v.toFixed(6) : '—'
}

function fmtAlt(v) {
  return v != null ? `${v.toFixed(1)} m` : '—'
}

const DEFAULT_SPECIES_KEY = '__default__'

function fmtFreqBand(low, high) {
  if (low == null && high == null) return '—'
  return `${low != null ? low : '—'}–${high != null ? high : '—'} Hz`
}

// Blank editable-fields form, used for both the inline edit form (seeded from
// a row) and the add-species form (seeded with factory-default-shaped values).
function fieldsFromRow(row) {
  return {
    enabled: row.enabled,
    correlationMethod: row.correlationMethod,
    onsetDetectionMethod: row.onsetDetectionMethod,
    freqBandLowHz: row.freqBandLowHz != null ? String(row.freqBandLowHz) : '',
    freqBandHighHz: row.freqBandHighHz != null ? String(row.freqBandHighHz) : '',
    pullWindowS: String(row.pullWindowS),
    windowMarginPreMs: String(row.windowMarginPreMs),
    windowMarginPostMs: String(row.windowMarginPostMs),
    minCorroboratingNodes: String(row.minCorroboratingNodes),
    notes: row.notes ?? '',
  }
}

const BLANK_ADD_FIELDS = {
  enabled: true,
  correlationMethod: 'gcc_phat',
  onsetDetectionMethod: 'global_peak',
  freqBandLowHz: '',
  freqBandHighHz: '',
  pullWindowS: '3.0',
  windowMarginPreMs: '500',
  windowMarginPostMs: '500',
  minCorroboratingNodes: '4',
  notes: '',
}

// Validates + converts a fields-form (all-strings, as held in input state)
// into the JSON body shape the PUT endpoint expects. Returns [body, null] or
// [null, errorMessage].
function buildSpeciesParamsBody(fields) {
  const pullWindowS = parseFloat(fields.pullWindowS)
  const windowMarginPreMs = parseFloat(fields.windowMarginPreMs)
  const windowMarginPostMs = parseFloat(fields.windowMarginPostMs)
  const minCorroboratingNodes = parseInt(fields.minCorroboratingNodes, 10)
  const freqBandLowHz = fields.freqBandLowHz.trim() === '' ? null : parseFloat(fields.freqBandLowHz)
  const freqBandHighHz = fields.freqBandHighHz.trim() === '' ? null : parseFloat(fields.freqBandHighHz)

  if (Number.isNaN(pullWindowS) || pullWindowS <= 0) return [null, 'Pull window must be a positive number of seconds.']
  if (Number.isNaN(windowMarginPreMs) || windowMarginPreMs < 0) return [null, 'Pre-margin must be a non-negative number of ms.']
  if (Number.isNaN(windowMarginPostMs) || windowMarginPostMs < 0) return [null, 'Post-margin must be a non-negative number of ms.']
  if (!Number.isInteger(minCorroboratingNodes) || minCorroboratingNodes < 4) return [null, 'Min corroborating nodes must be an integer >= 4 (the solver requires at least 4).']
  if (fields.freqBandLowHz.trim() !== '' && Number.isNaN(freqBandLowHz)) return [null, 'Freq band low must be a number, or blank.']
  if (fields.freqBandHighHz.trim() !== '' && Number.isNaN(freqBandHighHz)) return [null, 'Freq band high must be a number, or blank.']
  if (!fields.correlationMethod.trim()) return [null, 'Correlation method is required.']
  if (!fields.onsetDetectionMethod.trim()) return [null, 'Onset detection method is required.']

  return [{
    enabled: fields.enabled,
    correlationMethod: fields.correlationMethod.trim(),
    onsetDetectionMethod: fields.onsetDetectionMethod.trim(),
    freqBandLowHz, freqBandHighHz,
    pullWindowS, windowMarginPreMs, windowMarginPostMs, minCorroboratingNodes,
    notes: fields.notes.trim() === '' ? null : fields.notes.trim(),
  }, null]
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

  // ---------------------------------------------------------------------
  // Species TDOA params
  // ---------------------------------------------------------------------
  const [speciesParams, setSpeciesParams] = useState([])
  const [spLoading, setSpLoading] = useState(true)
  const [spError, setSpError] = useState(null)

  const [editingKey, setEditingKey] = useState(null)
  const [editFields, setEditFields] = useState(null)
  const [editError, setEditError] = useState(null)
  const [editSaving, setEditSaving] = useState(false)

  const [deleteConfirmKey, setDeleteConfirmKey] = useState(null)
  const [deleteBusy, setDeleteBusy] = useState(false)

  const [resetArmed, setResetArmed] = useState(false)
  const [resetBusy, setResetBusy] = useState(false)

  const [addKey, setAddKey] = useState('')
  const [addFields, setAddFields] = useState(BLANK_ADD_FIELDS)
  const [addError, setAddError] = useState(null)
  const [addBusy, setAddBusy] = useState(false)

  const loadSpeciesParams = () => {
    setSpLoading(true)
    setSpError(null)
    apiFetch('/species-tdoa-params')
      .then(async res => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
        setSpeciesParams(await res.json())
      })
      .catch(err => setSpError(err.message ?? String(err)))
      .finally(() => setSpLoading(false))
  }

  useEffect(() => { loadSpeciesParams() }, [])

  function openEdit(row) {
    setEditingKey(row.speciesKey)
    setEditFields(fieldsFromRow(row))
    setEditError(null)
  }
  function closeEdit() {
    setEditingKey(null)
    setEditFields(null)
    setEditError(null)
  }
  function setEditField(field, value) {
    setEditFields(f => ({ ...f, [field]: value }))
  }

  async function handleSaveEdit(speciesKey) {
    const [body, validationError] = buildSpeciesParamsBody(editFields)
    if (validationError) { setEditError(validationError); return }
    if (speciesKey === DEFAULT_SPECIES_KEY && !body.enabled) {
      setEditError('The __default__ row cannot be disabled — it is the fallback used when a species has no row of its own.')
      return
    }
    setEditSaving(true)
    setEditError(null)
    try {
      const res = await apiFetch(`/species-tdoa-params/${encodeURIComponent(speciesKey)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}))
        throw new Error(errBody.detail ?? `${res.status} ${res.statusText}`)
      }
      closeEdit()
      await loadSpeciesParams()
    } catch (err) {
      setEditError(err.message ?? String(err))
    } finally {
      setEditSaving(false)
    }
  }

  async function handleDelete(speciesKey) {
    setDeleteBusy(true)
    setSpError(null)
    try {
      const res = await apiFetch(`/species-tdoa-params/${encodeURIComponent(speciesKey)}`, { method: 'DELETE' })
      if (!res.ok && res.status !== 204) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail ?? `${res.status} ${res.statusText}`)
      }
      setDeleteConfirmKey(null)
      await loadSpeciesParams()
    } catch (err) {
      setSpError(err.message ?? String(err))
    } finally {
      setDeleteBusy(false)
    }
  }

  async function handleResetDefault() {
    if (!resetArmed) { setResetArmed(true); return }
    setResetBusy(true)
    setSpError(null)
    try {
      const res = await apiFetch('/species-tdoa-params/reset-default', { method: 'POST' })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail ?? `${res.status} ${res.statusText}`)
      }
      await loadSpeciesParams()
    } catch (err) {
      setSpError(err.message ?? String(err))
    } finally {
      setResetBusy(false)
      setResetArmed(false)
    }
  }

  function setAddField(field, value) {
    setAddFields(f => ({ ...f, [field]: value }))
  }

  async function handleAdd(e) {
    e.preventDefault()
    setAddError(null)
    const trimmedKey = addKey.trim()
    if (!trimmedKey) { setAddError('Species key is required — must match a detection\'s common name.'); return }
    if (trimmedKey === DEFAULT_SPECIES_KEY) { setAddError(`"${DEFAULT_SPECIES_KEY}" already exists — edit it in the table above.`); return }
    if (speciesParams.some(r => r.speciesKey === trimmedKey)) { setAddError('That species already has a row — edit it in the table above instead.'); return }
    const [body, validationError] = buildSpeciesParamsBody(addFields)
    if (validationError) { setAddError(validationError); return }

    setAddBusy(true)
    try {
      const res = await apiFetch(`/species-tdoa-params/${encodeURIComponent(trimmedKey)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}))
        throw new Error(errBody.detail ?? `${res.status} ${res.statusText}`)
      }
      setAddKey('')
      setAddFields(BLANK_ADD_FIELDS)
      await loadSpeciesParams()
    } catch (err) {
      setAddError(err.message ?? String(err))
    } finally {
      setAddBusy(false)
    }
  }

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
    wideSection: {
      background: 'var(--surface1, #1e1e1e)',
      border: '1px solid var(--border, #333)',
      borderRadius: 8, padding: '12px 16px',
      maxWidth: 900,
    },
    table: { width: '100%', borderCollapse: 'collapse', fontSize: 12, marginTop: 10 },
    th: {
      textAlign: 'left', padding: '6px 10px',
      fontSize: 10, fontWeight: 600, letterSpacing: '0.05em', textTransform: 'uppercase',
      color: 'var(--text-muted, #888)',
      borderBottom: '1px solid var(--border, #333)',
    },
    tr: { borderBottom: '1px solid var(--border, #2a2a2a)' },
    td: { padding: '8px 10px', color: 'var(--text, #eee)', verticalAlign: 'middle' },
    actionBtn: {
      fontSize: 11, color: 'var(--text-secondary, #aaa)',
      background: 'transparent',
      border: '1px solid var(--border, #333)',
      borderRadius: 4, padding: '4px 10px', cursor: 'pointer',
      marginLeft: 4,
    },
    dangerBtn: { color: 'var(--red, #f44336)', borderColor: 'rgba(244,67,54,0.3)' },
    primaryBtn: { background: 'var(--accent, #4da6ff)', color: '#fff', border: 'none' },
    smallInput: {
      background: 'var(--surface2, #2a2a2a)',
      border: '1px solid var(--border, #333)',
      borderRadius: 4, padding: '5px 7px',
      color: 'var(--text, #eee)', fontSize: 12,
      boxSizing: 'border-box',
    },
    fieldGroup: { display: 'flex', flexDirection: 'column', gap: 3 },
    defaultBadge: {
      fontSize: 10, fontWeight: 600, letterSpacing: '0.04em',
      color: 'var(--accent, #4da6ff)',
      background: 'rgba(77,166,255,0.12)',
      border: '1px solid rgba(77,166,255,0.25)',
      borderRadius: 3, padding: '1px 5px',
      marginLeft: 6,
    },
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

      <div style={sty.wideSection}>
        <div style={sty.label}>Species TDOA Parameters</div>
        <div style={{ ...sty.hint, marginBottom: 4 }}>
          Per-species tuning for the TDOA orchestration pipeline (onset detection, correlation
          method, pull window/margins, minimum corroborating nodes). The <code>{DEFAULT_SPECIES_KEY}</code> row
          is the fallback used when a detected species has no row of its own, or its row is disabled —
          it can't be deleted or disabled, only edited or reset to factory defaults.
        </div>

        {spError && (
          <div style={{
            fontSize: 12, color: 'var(--red, #f44336)',
            background: 'rgba(244,67,54,0.12)', borderRadius: 4, padding: '6px 8px',
            marginTop: 8,
          }}>
            {spError}
          </div>
        )}

        {spLoading ? (
          <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 10 }}>Loading…</div>
        ) : (
          <table style={sty.table}>
            <thead>
              <tr>
                <th style={sty.th}>Species</th>
                <th style={sty.th}>Enabled</th>
                <th style={sty.th}>Methods</th>
                <th style={sty.th}>Freq band</th>
                <th style={sty.th}>Pull / margins</th>
                <th style={sty.th}>Min nodes</th>
                <th style={sty.th}>Updated</th>
                <th style={{ ...sty.th, textAlign: 'right' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {speciesParams.map(row => {
                const isDefault = row.speciesKey === DEFAULT_SPECIES_KEY
                const isEditing = editingKey === row.speciesKey
                return (
                  <>
                    <tr key={row.speciesKey} style={sty.tr}>
                      <td style={sty.td}>
                        {row.speciesKey}
                        {isDefault && <span style={sty.defaultBadge}>fallback</span>}
                      </td>
                      <td style={sty.td}>{row.enabled ? 'Yes' : 'No'}</td>
                      <td style={sty.td}>{row.correlationMethod} / {row.onsetDetectionMethod}</td>
                      <td style={sty.td}>{fmtFreqBand(row.freqBandLowHz, row.freqBandHighHz)}</td>
                      <td style={sty.td}>{row.pullWindowS}s / ±{row.windowMarginPreMs}–{row.windowMarginPostMs}ms</td>
                      <td style={sty.td}>{row.minCorroboratingNodes}</td>
                      <td style={{ ...sty.td, color: 'var(--text-muted, #888)', fontSize: 11 }}>
                        {row.updatedAt ? new Date(row.updatedAt).toLocaleString() : '—'}
                      </td>
                      <td style={{ ...sty.td, textAlign: 'right' }}>
                        {!isEditing && (
                          <button style={sty.actionBtn} onClick={() => openEdit(row)}>Edit</button>
                        )}
                        {!isDefault && (
                          deleteConfirmKey === row.speciesKey ? (
                            <>
                              <span style={{ fontSize: 11, color: 'var(--text-muted)', marginRight: 6 }}>Sure?</span>
                              <button style={{ ...sty.actionBtn, ...sty.dangerBtn }} disabled={deleteBusy}
                                onClick={() => handleDelete(row.speciesKey)}>Yes, delete</button>
                              <button style={sty.actionBtn} onClick={() => setDeleteConfirmKey(null)}>Cancel</button>
                            </>
                          ) : (
                            <button style={{ ...sty.actionBtn, ...sty.dangerBtn }}
                              onClick={() => setDeleteConfirmKey(row.speciesKey)}>Delete</button>
                          )
                        )}
                        {isDefault && (
                          <button style={sty.actionBtn} disabled={resetBusy}
                            onClick={handleResetDefault} onBlur={() => setResetArmed(false)}>
                            {resetBusy ? 'Resetting…' : resetArmed ? 'Click again to confirm' : 'Reset to factory defaults'}
                          </button>
                        )}
                      </td>
                    </tr>

                    {isEditing && (
                      <tr key={`${row.speciesKey}-edit`} style={{ background: 'var(--surface2, #2a2a2a)' }}>
                        <td colSpan={8} style={{ padding: '12px' }}>
                          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0,1fr))', gap: 10, marginBottom: 10 }}>
                            <label style={sty.fieldGroup}>
                              <span style={sty.fieldLabelText}>Enabled</span>
                              <select style={sty.smallInput} value={editFields.enabled ? '1' : '0'}
                                disabled={isDefault}
                                onChange={e => setEditField('enabled', e.target.value === '1')}>
                                <option value="1">Yes</option>
                                <option value="0">No</option>
                              </select>
                            </label>
                            <label style={sty.fieldGroup}>
                              <span style={sty.fieldLabelText}>Correlation method</span>
                              <input style={sty.smallInput} value={editFields.correlationMethod}
                                onChange={e => setEditField('correlationMethod', e.target.value)} />
                            </label>
                            <label style={sty.fieldGroup}>
                              <span style={sty.fieldLabelText}>Onset detection method</span>
                              <input style={sty.smallInput} value={editFields.onsetDetectionMethod}
                                onChange={e => setEditField('onsetDetectionMethod', e.target.value)} />
                            </label>
                            <label style={sty.fieldGroup}>
                              <span style={sty.fieldLabelText}>Min corroborating nodes</span>
                              <input style={sty.smallInput} type="number" min={4} step={1}
                                value={editFields.minCorroboratingNodes}
                                onChange={e => setEditField('minCorroboratingNodes', e.target.value)} />
                            </label>
                            <label style={sty.fieldGroup}>
                              <span style={sty.fieldLabelText}>Freq band low (Hz)</span>
                              <input style={sty.smallInput} type="number" step="any" placeholder="blank = no floor"
                                value={editFields.freqBandLowHz}
                                onChange={e => setEditField('freqBandLowHz', e.target.value)} />
                            </label>
                            <label style={sty.fieldGroup}>
                              <span style={sty.fieldLabelText}>Freq band high (Hz)</span>
                              <input style={sty.smallInput} type="number" step="any" placeholder="blank = no ceiling"
                                value={editFields.freqBandHighHz}
                                onChange={e => setEditField('freqBandHighHz', e.target.value)} />
                            </label>
                            <label style={sty.fieldGroup}>
                              <span style={sty.fieldLabelText}>Pull window (s)</span>
                              <input style={sty.smallInput} type="number" step="any" min={0}
                                value={editFields.pullWindowS}
                                onChange={e => setEditField('pullWindowS', e.target.value)} />
                            </label>
                            <label style={sty.fieldGroup}>
                              <span style={sty.fieldLabelText}>Margin pre (ms)</span>
                              <input style={sty.smallInput} type="number" step="any" min={0}
                                value={editFields.windowMarginPreMs}
                                onChange={e => setEditField('windowMarginPreMs', e.target.value)} />
                            </label>
                            <label style={sty.fieldGroup}>
                              <span style={sty.fieldLabelText}>Margin post (ms)</span>
                              <input style={sty.smallInput} type="number" step="any" min={0}
                                value={editFields.windowMarginPostMs}
                                onChange={e => setEditField('windowMarginPostMs', e.target.value)} />
                            </label>
                            <label style={{ ...sty.fieldGroup, gridColumn: 'span 2' }}>
                              <span style={sty.fieldLabelText}>Notes</span>
                              <input style={sty.smallInput} value={editFields.notes}
                                onChange={e => setEditField('notes', e.target.value)} />
                            </label>
                          </div>
                          <div style={sty.row}>
                            <button style={{ ...sty.actionBtn, ...sty.primaryBtn }} disabled={editSaving}
                              onClick={() => handleSaveEdit(row.speciesKey)}>
                              {editSaving ? 'Saving…' : 'Save'}
                            </button>
                            <button style={sty.actionBtn} onClick={closeEdit}>Cancel</button>
                            {editError && (
                              <span style={{ fontSize: 11, color: 'var(--red, #f44336)' }}>{editError}</span>
                            )}
                          </div>
                        </td>
                      </tr>
                    )}
                  </>
                )
              })}

              {speciesParams.length === 0 && (
                <tr>
                  <td colSpan={8} style={{ ...sty.td, color: 'var(--text-muted, #888)', textAlign: 'center', padding: 24 }}>
                    No species TDOA params configured yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}

        <div style={{ marginTop: 16, paddingTop: 14, borderTop: '1px solid var(--border, #333)' }}>
          <div style={{ ...sty.label, marginBottom: 10 }}>Add species</div>
          <form onSubmit={handleAdd} style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0,1fr))', gap: 10 }}>
            <label style={sty.fieldGroup}>
              <span style={sty.fieldLabelText}>Species key (common name)</span>
              <input style={sty.smallInput} value={addKey} disabled={addBusy}
                placeholder="e.g. Pheasant Coucal"
                onChange={e => setAddKey(e.target.value)} />
            </label>
            <label style={sty.fieldGroup}>
              <span style={sty.fieldLabelText}>Correlation method</span>
              <input style={sty.smallInput} value={addFields.correlationMethod} disabled={addBusy}
                onChange={e => setAddField('correlationMethod', e.target.value)} />
            </label>
            <label style={sty.fieldGroup}>
              <span style={sty.fieldLabelText}>Onset detection method</span>
              <input style={sty.smallInput} value={addFields.onsetDetectionMethod} disabled={addBusy}
                onChange={e => setAddField('onsetDetectionMethod', e.target.value)} />
            </label>
            <label style={sty.fieldGroup}>
              <span style={sty.fieldLabelText}>Min corroborating nodes</span>
              <input style={sty.smallInput} type="number" min={4} step={1} disabled={addBusy}
                value={addFields.minCorroboratingNodes}
                onChange={e => setAddField('minCorroboratingNodes', e.target.value)} />
            </label>
            <label style={sty.fieldGroup}>
              <span style={sty.fieldLabelText}>Freq band low (Hz)</span>
              <input style={sty.smallInput} type="number" step="any" disabled={addBusy} placeholder="blank = no floor"
                value={addFields.freqBandLowHz}
                onChange={e => setAddField('freqBandLowHz', e.target.value)} />
            </label>
            <label style={sty.fieldGroup}>
              <span style={sty.fieldLabelText}>Freq band high (Hz)</span>
              <input style={sty.smallInput} type="number" step="any" disabled={addBusy} placeholder="blank = no ceiling"
                value={addFields.freqBandHighHz}
                onChange={e => setAddField('freqBandHighHz', e.target.value)} />
            </label>
            <label style={sty.fieldGroup}>
              <span style={sty.fieldLabelText}>Pull window (s)</span>
              <input style={sty.smallInput} type="number" step="any" min={0} disabled={addBusy}
                value={addFields.pullWindowS}
                onChange={e => setAddField('pullWindowS', e.target.value)} />
            </label>
            <label style={sty.fieldGroup}>
              <span style={sty.fieldLabelText}>Margin pre (ms)</span>
              <input style={sty.smallInput} type="number" step="any" min={0} disabled={addBusy}
                value={addFields.windowMarginPreMs}
                onChange={e => setAddField('windowMarginPreMs', e.target.value)} />
            </label>
            <label style={sty.fieldGroup}>
              <span style={sty.fieldLabelText}>Margin post (ms)</span>
              <input style={sty.smallInput} type="number" step="any" min={0} disabled={addBusy}
                value={addFields.windowMarginPostMs}
                onChange={e => setAddField('windowMarginPostMs', e.target.value)} />
            </label>
            <label style={sty.fieldGroup}>
              <span style={sty.fieldLabelText}>Notes</span>
              <input style={sty.smallInput} disabled={addBusy}
                value={addFields.notes}
                onChange={e => setAddField('notes', e.target.value)} />
            </label>
            <div style={{ display: 'flex', alignItems: 'flex-end' }}>
              <button type="submit" style={{ ...sty.actionBtn, ...sty.primaryBtn, padding: '7px 16px' }} disabled={addBusy}>
                {addBusy ? 'Adding…' : 'Add species'}
              </button>
            </div>
          </form>
          {addError && (
            <div style={{ fontSize: 11, color: 'var(--red, #f44336)', marginTop: 8 }}>{addError}</div>
          )}
        </div>
      </div>
    </div>
  )
}
