import NodeCard from './NodeCard.jsx'

export default function NodeSidebar({ nodes, selectedId, onSelect }) {
  const primary = nodes.filter(n => n.role === 'PRIMARY')
  const leaves  = nodes.filter(n => n.role === 'LEAF')

  return (
    <div style={{
      width: 268,
      flexShrink: 0,
      background: 'var(--bg-panel)',
      borderRight: '1px solid var(--border)',
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
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          {nodes.filter(n => n.status === 'online').length}/{nodes.length} online
        </span>
      </div>

      {/* Node list */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '10px 8px', display: 'flex', flexDirection: 'column', gap: 6 }}>
        {primary.length > 0 && (
          <div style={{ marginBottom: 2 }}>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', padding: '0 4px 4px', letterSpacing: '0.06em', textTransform: 'uppercase' }}>
              Primary
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {primary.map(node => (
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
            <div style={{ fontSize: 10, color: 'var(--text-muted)', padding: '0 4px 4px', letterSpacing: '0.06em', textTransform: 'uppercase' }}>
              Leaf Nodes
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
      </div>
    </div>
  )
}
