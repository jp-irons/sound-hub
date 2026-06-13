import { useState, useEffect, useCallback, useRef } from 'react'
import { apiFetch } from '../auth.js'

const API_BASE = '/api'
const POLL_INTERVAL_MS = 5000

const CONF_HIGH  = 0.8
const CONF_MED   = 0.5

function confidenceColour(conf) {
  if (conf >= CONF_HIGH) return 'var(--green,  #4caf50)'
  if (conf >= CONF_MED)  return 'var(--yellow, #ffc107)'
  return 'var(--red, #f44336)'
}

function ConfBar({ value }) {
  const pct = Math.round(value * 100)
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <div style={{
        width: 80, height: 8, background: 'var(--surface2, #2a2a2a)',
        borderRadius: 4, overflow: 'hidden',
      }}>
        <div style={{
          width: `${pct}%`, height: '100%',
          background: confidenceColour(value),
          borderRadius: 4,
          transition: 'width 0.2s',
        }} />
      </div>
      <span style={{ fontSize: 11, color: 'var(--text-muted, #888)', minWidth: 32 }}>
        {pct}%
      </span>
    </div>
  )
}

function formatTime(iso) {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch { return iso }
}

export default function DetectionsTab({ isAdmin = false }) {
  const [detections, setDetections]   = useState([])
  const [uploading, setUploading]     = useState(false)
  const [uploadResult, setUploadResult] = useState(null) // {ok, message}
  const [dragging, setDragging]       = useState(false)
  const [minConf, setMinConf]         = useState(0.0)
  const [species, setSpecies]         = useState('')
  const [useGeo, setUseGeo]           = useState(false)
  const [uploadConf, setUploadConf]   = useState(0.5)
  const fileInputRef = useRef()

  const fetchDetections = useCallback(async () => {
    try {
      const params = new URLSearchParams({ limit: 200, min_conf: minConf })
      if (species.trim()) params.set('species', species.trim())
      const res = await fetch(`${API_BASE}/detections?${params}`)
      if (!res.ok) throw new Error(`${res.status}`)
      setDetections(await res.json())
    } catch { /* backend may not be up yet */ }
  }, [minConf, species])

  useEffect(() => {
    fetchDetections()
    const t = setInterval(fetchDetections, POLL_INTERVAL_MS)
    return () => clearInterval(t)
  }, [fetchDetections])

  async function handleUpload(file) {
    if (!file) return
    setUploading(true)
    setUploadResult(null)
    try {
      const form = new FormData()
      form.append('file', file)
      const params = new URLSearchParams({ geo: useGeo, min_conf: uploadConf })
      const res = await apiFetch(`/detections/analyze?${params}`, {
        method: 'POST',
        body: form,
      })
      const data = await res.json()
      if (!res.ok) {
        setUploadResult({ ok: false, message: data?.detail ?? `HTTP ${res.status}` })
      } else {
        setUploadResult({
          ok: true,
          message: data.length
            ? `${data.length} detection${data.length !== 1 ? 's' : ''} found in ${file.name}`
            : `No detections above ${Math.round(uploadConf * 100)}% confidence in ${file.name}`,
        })
        fetchDetections()
      }
    } catch (err) {
      setUploadResult({ ok: false, message: String(err) })
    } finally {
      setUploading(false)
    }
  }

  function onFileChange(e) {
    const file = e.target.files?.[0]
    if (file) handleUpload(file)
    e.target.value = ''
  }

  function onDrop(e) {
    e.preventDefault()
    setDragging(false)
    const file = e.dataTransfer.files?.[0]
    if (file) handleUpload(file)
  }

  const sty = {
    root: {
      display: 'flex', flexDirection: 'column', height: '100%',
      padding: '16px 20px', gap: 16, boxSizing: 'border-box',
      overflow: 'hidden',
    },
    section: {
      background: 'var(--surface1, #1e1e1e)',
      border: '1px solid var(--border, #333)',
      borderRadius: 8, padding: '12px 16px',
    },
    label: { fontSize: 11, color: 'var(--text-muted, #888)', marginBottom: 6 },
    row: { display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' },
    input: {
      background: 'var(--surface2, #2a2a2a)',
      border: '1px solid var(--border, #333)',
      borderRadius: 4, padding: '4px 8px',
      color: 'var(--text, #eee)', fontSize: 12,
    },
    dropZone: {
      border: `2px dashed ${dragging ? 'var(--accent, #4da6ff)' : 'var(--border, #444)'}`,
      borderRadius: 8, padding: '20px 24px',
      textAlign: 'center', cursor: 'pointer',
      background: dragging ? 'var(--surface2, #2a2a2a)' : 'transparent',
      transition: 'all 0.15s',
    },
    table: { width: '100%', borderCollapse: 'collapse', fontSize: 12 },
    th: {
      textAlign: 'left', padding: '6px 10px',
      borderBottom: '1px solid var(--border, #333)',
      color: 'var(--text-muted, #888)', fontWeight: 500,
      position: 'sticky', top: 0,
      background: 'var(--surface1, #1e1e1e)',
    },
    td: {
      padding: '6px 10px',
      borderBottom: '1px solid var(--border-faint, #2a2a2a)',
      color: 'var(--text, #eee)',
    },
  }

  return (
    <div style={sty.root}>
      {/* ── Upload — admin only ── */}
      {isAdmin && <div style={sty.section}>
        <div style={sty.label}>ANALYSE WAV FILE</div>
        <div style={sty.row}>
          {/* Upload options */}
          <label style={{ ...sty.row, gap: 6, cursor: 'pointer', fontSize: 12, color: 'var(--text, #eee)' }}>
            <input
              type="checkbox"
              checked={useGeo}
              onChange={e => setUseGeo(e.target.checked)}
            />
            Brisbane geo filter
          </label>
          <label style={{ fontSize: 12, color: 'var(--text-muted, #888)', display: 'flex', alignItems: 'center', gap: 6 }}>
            Threshold
            <input
              type="number" min="0" max="1" step="0.05"
              value={uploadConf}
              onChange={e => setUploadConf(parseFloat(e.target.value))}
              style={{ ...sty.input, width: 60 }}
            />
          </label>
        </div>

        <div style={{ marginTop: 10 }}>
          <div
            style={sty.dropZone}
            onClick={() => fileInputRef.current?.click()}
            onDragOver={e => { e.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
          >
            {uploading
              ? <span style={{ color: 'var(--text-muted, #888)' }}>Analysing…</span>
              : <span style={{ color: 'var(--text-muted, #888)' }}>
                  Drop a WAV here or <span style={{ color: 'var(--accent, #4da6ff)' }}>click to browse</span>
                </span>
            }
          </div>
          <input
            ref={fileInputRef}
            type="file" accept=".wav,audio/wav"
            style={{ display: 'none' }}
            onChange={onFileChange}
          />
        </div>

        {uploadResult && (
          <div style={{
            marginTop: 8, fontSize: 12, padding: '6px 10px', borderRadius: 4,
            background: uploadResult.ok
              ? 'rgba(76,175,80,0.12)' : 'rgba(244,67,54,0.12)',
            color: uploadResult.ok
              ? 'var(--green, #4caf50)' : 'var(--red, #f44336)',
          }}>
            {uploadResult.message}
          </div>
        )}
      </div>}

      {/* ── Filter bar ── */}
      <div style={{ ...sty.row, gap: 12 }}>
        <label style={{ fontSize: 12, color: 'var(--text-muted, #888)', display: 'flex', alignItems: 'center', gap: 6 }}>
          Species filter
          <input
            type="text" placeholder="e.g. Kookaburra"
            value={species}
            onChange={e => setSpecies(e.target.value)}
            style={{ ...sty.input, width: 160 }}
          />
        </label>
        <label style={{ fontSize: 12, color: 'var(--text-muted, #888)', display: 'flex', alignItems: 'center', gap: 6 }}>
          Min confidence
          <input
            type="number" min="0" max="1" step="0.05"
            value={minConf}
            onChange={e => setMinConf(parseFloat(e.target.value))}
            style={{ ...sty.input, width: 60 }}
          />
        </label>
        <span style={{ fontSize: 11, color: 'var(--text-muted, #888)' }}>
          {detections.length} record{detections.length !== 1 ? 's' : ''}
        </span>
      </div>

      {/* ── Table ── */}
      <div style={{ flex: 1, overflow: 'auto', background: 'var(--surface1, #1e1e1e)', borderRadius: 8, border: '1px solid var(--border, #333)' }}>
        {detections.length === 0
          ? (
            <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted, #888)', fontSize: 13 }}>
              No detections yet — upload a WAV file or run the live mic script.
            </div>
          )
          : (
            <table style={sty.table}>
              <thead>
                <tr>
                  <th style={sty.th}>Time</th>
                  <th style={sty.th}>Common name</th>
                  <th style={sty.th}>Scientific name</th>
                  <th style={sty.th}>Confidence</th>
                  <th style={sty.th}>Source</th>
                  <th style={sty.th}>Offset</th>
                </tr>
              </thead>
              <tbody>
                {detections.map(d => (
                  <tr key={d.id} style={{ cursor: 'default' }}>
                    <td style={sty.td}>{formatTime(d.analyzedAt)}</td>
                    <td style={{ ...sty.td, fontWeight: 500 }}>{d.commonName}</td>
                    <td style={{ ...sty.td, fontStyle: 'italic', color: 'var(--text-muted, #888)' }}>{d.scientificName}</td>
                    <td style={sty.td}><ConfBar value={d.confidence} /></td>
                    <td style={{ ...sty.td, color: 'var(--text-muted, #888)' }}>{d.source ?? '—'}</td>
                    <td style={{ ...sty.td, color: 'var(--text-muted, #888)' }}>
                      {d.startSec != null ? `${d.startSec}–${d.endSec}s` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        }
      </div>
    </div>
  )
}
