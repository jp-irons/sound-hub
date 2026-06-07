import { MapContainer, TileLayer, CircleMarker, Popup, useMap } from 'react-leaflet'
import { useEffect } from 'react'
import { GOD_NODE_LATLON } from '../data/mockNodes.js'

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

export default function MapView({ nodes, selectedId, onSelect }) {
  const center = [GOD_NODE_LATLON.lat, GOD_NODE_LATLON.lon]

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
          if (!node.latLon) return null  // node-3 position unknown

          const isSelected = node.id === selectedId
          const color = STATUS_COLOR[node.status]
          const relPos = node.positionRelative

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
              eventHandlers={{ click: () => onSelect(node.id) }}
            >
              <Popup>
                <div style={{ minWidth: 140 }}>
                  <strong>{node.hostname}</strong>
                  <div style={{ fontSize: 12, marginTop: 4, color: '#555' }}>
                    {node.role} · {node.status}
                  </div>
                  {relPos && (
                    <div style={{ fontSize: 11, marginTop: 4, color: '#777', fontFamily: 'monospace' }}>
                      E {relPos.eM.toFixed(1)}m · N {relPos.nM.toFixed(1)}m · Alt {relPos.altM > 0 ? '+' : ''}{relPos.altM.toFixed(1)}m
                    </div>
                  )}
                  <div style={{ fontSize: 11, marginTop: 4, color: '#777' }}>
                    {node.ipAddress}
                  </div>
                </div>
              </Popup>
            </CircleMarker>
          )
        })}
      </MapContainer>

      {/* Overlay: uncalibrated node warning */}
      {nodes.some(n => !n.positionKnown) && (
        <div style={{
          position: 'absolute', bottom: 16, left: '50%', transform: 'translateX(-50%)',
          background: 'rgba(210,153,34,0.15)',
          border: '1px solid var(--yellow)',
          color: 'var(--yellow)',
          padding: '6px 12px', borderRadius: 6,
          fontSize: 12, zIndex: 1000,
          backdropFilter: 'blur(4px)',
          pointerEvents: 'none',
        }}>
          {nodes.filter(n => !n.positionKnown).map(n => n.hostname).join(', ')} — position unknown · TOF calibration required
        </div>
      )}
    </div>
  )
}
