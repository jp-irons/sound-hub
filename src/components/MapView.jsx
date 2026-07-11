import { MapContainer, TileLayer, CircleMarker, Popup, useMap } from 'react-leaflet'
import { useEffect, useState } from 'react'

const API_BASE = '/api'

const displayStatus = s => s === 'online' ? 'healthy' : s

const STATUS_COLOR = {
  online:   '#3fb950',
  degraded: '#d29922',
  offline:  '#f85149',
}

// Syncs map view when selected node changes
function MapFocus({ nodes, selectedId }) {
  const map = useMap()
  useEffect(() => {
    const node = nodes.find(n => n.id === selectedId)
    if (node?.latLon) {
      map.setView([node.latLon.lat, node.latLon.lon], map.getZoom(), { animate: true })
    }
  }, [selectedId, nodes, map])
  return null
}

// Fallback map centre used before the hub array origin is configured.
const PROPERTY_FALLBACK = [-27.497347, 152.996641]

/**
 * props:
 *   nodes       — array of node objects (full NodeView or slim PublicNodeView)
 *   selectedId  — currently selected node id (authenticated only, else null)
 *   onSelectNode — callback(id) when a node is clicked (authenticated only, else null)
 *   selectable  — true when authenticated; false for unauthenticated browsing.
 *                 When false, clicking a node opens a minimal Popup instead of
 *                 selecting it, and no sensitive fields (IP, relPos) are shown.
 */
export default function MapView({ nodes, selectedId, onSelectNode, selectable = true }) {
  const [arrayOrigin, setArrayOrigin] = useState(null)

  // Use the public/origin endpoint — no auth required, safe for unauthenticated map.
  useEffect(() => {
    fetch(`${API_BASE}/public/origin`)
      .then(res => res.ok ? res.json() : null)
      .then(data => { if (data) setArrayOrigin(data) })
      .catch(() => {})
  }, [])

  const center = arrayOrigin
    ? [arrayOrigin.lat, arrayOrigin.lon]
    : PROPERTY_FALLBACK

  return (
    <div style={{ flex: 1, position: 'relative' }}>
      <MapContainer
        center={center}
        zoom={20}
        style={{ width: '100%', height: '100%' }}
        zoomControl={true}
      >
        {/* Esri World Imagery — good for property-scale work */}
        <TileLayer
          url="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
          attribution="Tiles &copy; Esri"
          maxZoom={22}
          maxNativeZoom={19}
        />

        <MapFocus nodes={nodes} selectedId={selectedId} />

        {nodes.map(node => {
          // Brokers don't belong on the map even if they happen to have
          // stored position data (e.g. a node switched to broker after
          // being surveyed) — its role is comms relay, not a sensing
          // array member.
          if (!node.latLon || node.role === 'BROKER') return null

          const isSelected = node.id === selectedId
          const color = STATUS_COLOR[node.status] ?? STATUS_COLOR.offline
          const relPos = node.positionRelative  // undefined for PublicNodeView

          return (
            <CircleMarker
              key={node.id}
              center={[node.latLon.lat, node.latLon.lon]}
              radius={isSelected ? 10 : 7}
              pathOptions={{
                color: isSelected ? '#fff' : color,
                fillColor: color,
                fillOpacity: 0.9,
                weight: isSelected ? 2.5 : 1.5,
              }}
              eventHandlers={selectable ? { click: () => onSelectNode?.(node.id) } : {}}
            >
              <Popup>
                {selectable ? (
                  /* Authenticated — full details */
                  <div style={{ minWidth: 140 }}>
                    <strong>{node.hostname}</strong>
                    <div style={{ fontSize: 12, marginTop: 4, color: '#555' }}>
                      {displayStatus(node.status)}{[node.role === 'BROKER' && 'BROKER'].filter(Boolean).map(l => ` · ${l}`)}
                    </div>
                    {relPos && (
                      <div style={{ fontSize: 11, marginTop: 4, color: '#777', fontFamily: 'monospace' }}>
                        N {relPos.nM.toFixed(1)}m · E {relPos.eM.toFixed(1)}m · Alt {relPos.altM > 0 ? '+' : ''}{relPos.altM.toFixed(1)}m
                      </div>
                    )}
                    <div style={{ fontSize: 11, marginTop: 4, color: '#777' }}>
                      <a href={`https://${node.ipAddress}`} target={node.id} style={{ color: 'inherit' }}>
                        {node.ipAddress}
                      </a>
                    </div>
                  </div>
                ) : (
                  /* Unauthenticated — minimal tooltip, no sensitive fields */
                  <div style={{ minWidth: 110 }}>
                    <strong>{node.hostname}</strong>
                    <div style={{ fontSize: 12, marginTop: 4, color: '#555' }}>
                      {displayStatus(node.status)}
                    </div>
                  </div>
                )}
              </Popup>
            </CircleMarker>
          )
        })}
      </MapContainer>

      {/* Overlay: uncalibrated node warning — brokers excluded, they don't
          need array-geometry calibration regardless of position status. */}
      {nodes.some(n => n.role !== 'BROKER' && !n.positionKnown) && (
        <div style={{
          position: 'absolute', bottom: 16, left: '50%', transform: 'translateX(-50%)',
          background: 'rgba(210,153,34,0.15)',
          border: '1px solid rgba(210,153,34,0.4)',
          borderRadius: 6, padding: '6px 14px',
          fontSize: 12, zIndex: 1000,
          backdropFilter: 'blur(4px)',
          pointerEvents: 'none',
          color: 'rgba(210,153,34,0.9)',
        }}>
          {nodes.filter(n => n.role !== 'BROKER' && !n.positionKnown).map(n => n.hostname).join(', ')} — position unknown · calibration required
        </div>
      )}
    </div>
  )
}
