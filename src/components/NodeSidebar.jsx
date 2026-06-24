import { useState } from 'react'
import NodeCard from './NodeCard.jsx'

// Compact row for pending/rejected nodes.
// Action buttons are only shown to admins.
function AdmissionRow({ node, onSelect, onApprove, onReject, variant, isAdmin }) {
  return (
    <div
      onClick={() => onSelect(node.id)}
      style={{
        padding: '8px 10px',
        borderLeft: `3px solid ${variant === 'pending' ? 'var(--yellow)' : 'var(--text-muted)'}`,
        background: 'var(--bg-card)',
        borderRadius: '0 6px 6px 0',
        cursor: 'pointer',
        display: 'flex', flexDirection: 'column', gap: 6,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
        <span style={{ fontWeight: 600, fontSize: 12, flex: 1 }}>{node.hostname}</span>
        <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{node.ipAddress ?? '—'}</span>
      </div>
      {isAdmin && (
        <div style={{ display: 'flex', gap: 6 }}>
          {variant !== 'pending' && (
            <button
              className="btn"
              style={{ flex: 1, fontSize: 11, padding: '3px 8px' }}
              onClick={e => { e.stopPropagation(); onApprove(node.id) }}
              title="Re-approve this node"
            >
              Approve
            </button>
          )}
          {variant === 'pending' && (
            <>
              <button
                className="btn btn-primary"
                style={{ flex: 1, fontSize: 11, padding: '3px 8px' }}
                onClick={e => { e.stopPropagation(); onApprove(node.id) }}
                title="Admit this node into the active array"
              >
                Approve
              </button>
              <button
                className="btn"
                style={{ flex: 1, fontSize: 11, padding: '3px 8px', borderColor: 'var(--red)', color: 'var(--red)' }}
                onClick={e => { e.stopPropagation(); onReject(node.id) }}
                title="Decline this node"
              >
                Reject
              </button>
            </>
          )}
        </div>
      )}
    </div>
  )
}

export default function NodeSidebar({ nodes, selectedId, onSelect, onApprove, onReject, isAdmin = false }) {
  const [showRejected, setShowRejected] = useState(false)

  const approved = nodes.filter(n => n.approvalStatus === 'approved')
  const pending  = nodes.filter(n => n.approvalStatus === 'pending')
  const rejected = nodes.filter(n => n.approvalStatus === 'rejected')

  const anchors = approved.filter(n => n.role === 'BROKER')
  const leaves  = approved.filter(n => n.role !== 'BROKER')

  return (
    <div style={{
      flex: 1,
      background: 'var(--bg-panel)',
      display: 'flex',
      flexDirection: 'column',
      overflow: 'hidden',
    }}>
      {/* Header */}
      <div style={{
        padding: '10px 14px 8px',
        borderBottom: '1px solid var(--border-muted)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      }}>
        <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', letterSpacing: '0.04em', textTransform: 'uppercase' }}>
          Nodes
        </span>
        <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
          {approved.filter(n => n.status === 'online').length}/{approved.length} healthy
        </span>
      </div>

      {/* Node list */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '10px 8px', display: 'flex', flexDirection: 'column', gap: 6 }}>

        {/* Pending — only show section when admin */}
        {isAdmin && pending.length > 0 && (
          <div style={{ marginBottom: 2 }}>
            <div style={{ fontSize: 10, color: 'var(--yellow)', padding: '0 4px 4px', letterSpacing: '0.06em', textTransform: 'uppercase' }}>
              Pending approval ({pending.length})
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {pending.map(node => (
                <AdmissionRow
                  key={node.id}
                  node={node}
                  onSelect={onSelect}
                  onApprove={onApprove}
                  onReject={onReject}
                  variant="pending"
                  isAdmin={isAdmin}
                />
              ))}
            </div>
          </div>
        )}

        {anchors.length > 0 && (
          <div style={{ marginBottom: 2 }}>
            <div style={{ fontSize: 10, color: 'var(--text-secondary)', padding: '0 4px 4px', letterSpacing: '0.06em', textTransform: 'uppercase' }}>
              Anchor Nodes
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {anchors.map(node => (
                <NodeCard
                  key={node.id}
                  node={node}
                  selected={selectedId === node.id}
                  onSelect={onSelect}
                />
              ))}
            </div>
          </div>
        )}

        {leaves.length > 0 && (
          <div>
            <div style={{ fontSize: 10, color: 'var(--text-secondary)', padding: '0 4px 4px', letterSpacing: '0.06em', textTransform: 'uppercase' }}>
              Nodes
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {leaves.map(node => (
                <NodeCard
                  key={node.id}
                  node={node}
                  selected={selectedId === node.id}
                  onSelect={onSelect}
                />
              ))}
            </div>
          </div>
        )}

        {/* Rejected — visible to all, but re-approve action only for admins */}
        {rejected.length > 0 && (
          <div style={{ marginTop: 4, borderTop: '1px solid var(--border-muted)', paddingTop: 8 }}>
            <button
              onClick={() => setShowRejected(s => !s)}
              style={{
                background: 'none', border: 'none', cursor: 'pointer',
                fontSize: 10, color: 'var(--text-muted)', padding: '0 4px 4px',
                letterSpacing: '0.06em', textTransform: 'uppercase',
                display: 'flex', alignItems: 'center', gap: 5, width: '100%',
              }}
            >
              <span>{showRejected ? '▾' : '▸'}</span>
              Rejected ({rejected.length})
            </button>
            {showRejected && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {rejected.map(node => (
                  <AdmissionRow
                    key={node.id}
                    node={node}
                    onSelect={onSelect}
                    onApprove={onApprove}
                    onReject={onReject}
                    variant="rejected"
                    isAdmin={isAdmin}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
