import { useState, useRef } from 'react'
import { apiFetch } from '../auth.js'

export default function ToolsTab() {
  const [uploading, setUploading]       = useState(false)
  const [uploadResult, setUploadResult] = useState(null) // {ok, message}
  const [dragging, setDragging]         = useState(false)
  const [useGeo, setUseGeo]             = useState(false)
  const [uploadConf, setUploadConf]     = useState(0.5)
  const fileInputRef = useRef()

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
  }

  return (
    <div style={sty.root}>
      <div style={sty.section}>
        <div style={sty.label}>ANALYSE WAV FILE</div>
        <div style={sty.row}>
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
      </div>
    </div>
  )
}
