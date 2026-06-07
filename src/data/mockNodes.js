// Mock node data matching the actual system design:
// Node 0: god node (PRIMARY), deck level east, GPS on board
// Node 1: LEAF, roof level west (+2m), ESP-NOW synced
// Node 2: LEAF, ground level west (-4m), ESP-NOW synced
// Node 3: LEAF, ~5m forward of deck (-4m), position UNKNOWN pending TOF calibration

const now = () => new Date().toISOString();
const ago = (ms) => new Date(Date.now() - ms).toISOString();

// God node coordinates (deck, eastern end) — actual property location
const GOD_LAT = -27.497347389269983;
const GOD_LON = 152.9966414786787;
const M_PER_DEG_LAT = 111320;
const M_PER_DEG_LON = 98740;

function toLatLon(eM, nM) {
  return {
    lat: GOD_LAT + nM / M_PER_DEG_LAT,
    lon: GOD_LON + eM / M_PER_DEG_LON,
  };
}

export const MOCK_NODES = [
  {
    id: 'node-0',
    hostname: 'scn-node-0',
    role: 'PRIMARY',
    status: 'online',
    // God node is origin; absolute lat/lon used for map georeferencing only
    latLon: { lat: GOD_LAT, lon: GOD_LON },
    positionRelative: { eM: 0.0, nM: 0.0, altM: 0.0 },
    positionKnown: true,
    gps: {
      locked: true,
      satellites: 11,
      centroidN: 2847,
      centroidStddevM: 1.4,
      divergenceM: 0.6,
      divergenceN: 0.3,
      divergenceE: 0.5,
      divergenceAlt: 1.1,
    },
    clock: {
      source: 'GPS_NMEA',   // PPS hardware pending — stage 1 NMEA (~50ms)
      accuracyUs: 50000,
      offsetUs: null,
      kalmanSettled: null,
    },
    audio: {
      bufferCapacityS: 70,
      bufferUsedS: 52,
      sampleRateHz: 16000,
      bitDepth: 16,
      lastTriggerAt: null,
    },
    espNow: null,
    lastSeenAt: now(),
    ipAddress: '192.168.4.101',
    firmwareVersion: '0.3.1',
    flags: [],
  },
  {
    id: 'node-1',
    hostname: 'scn-node-1',
    role: 'LEAF',
    status: 'online',
    latLon: toLatLon(-8.2, -0.5),
    positionRelative: { eM: -8.2, nM: -0.5, altM: 2.0 },
    positionKnown: true,
    gps: null,
    clock: {
      source: 'ESPNOW_KALMAN',
      accuracyUs: 14,
      offsetUs: 312,
      kalmanSettled: true,
    },
    audio: {
      bufferCapacityS: 70,
      bufferUsedS: 68,
      sampleRateHz: 16000,
      bitDepth: 16,
      lastTriggerAt: ago(47000),
    },
    espNow: {
      rssi: -61,
      hopCount: 0,
      lastHeartbeatAt: ago(1800),
    },
    lastSeenAt: now(),
    ipAddress: '192.168.4.102',
    firmwareVersion: '0.3.1',
    flags: [],
  },
  {
    id: 'node-2',
    hostname: 'scn-node-2',
    role: 'LEAF',
    status: 'online',
    latLon: toLatLon(-8.0, -0.3),
    positionRelative: { eM: -8.0, nM: -0.3, altM: -4.0 },
    positionKnown: true,
    gps: null,
    clock: {
      source: 'ESPNOW_KALMAN',
      accuracyUs: 18,
      offsetUs: -145,
      kalmanSettled: true,
    },
    audio: {
      bufferCapacityS: 70,
      bufferUsedS: 70,
      sampleRateHz: 16000,
      bitDepth: 16,
      lastTriggerAt: ago(47200),
    },
    espNow: {
      rssi: -68,
      hopCount: 0,
      lastHeartbeatAt: ago(2100),
    },
    lastSeenAt: ago(1000),
    ipAddress: '192.168.4.103',
    firmwareVersion: '0.3.1',
    flags: [],
  },
  {
    id: 'node-3',
    hostname: 'scn-node-3',
    role: 'LEAF',
    status: 'degraded',   // online but position unknown + clock not settled
    latLon: null,         // position unknown — pending TOF calibration
    positionRelative: null,
    positionKnown: false,
    gps: null,
    clock: {
      source: 'ESPNOW_KALMAN',
      accuracyUs: 480,
      offsetUs: 891,
      kalmanSettled: false,
    },
    audio: {
      bufferCapacityS: 70,
      bufferUsedS: 31,
      sampleRateHz: 16000,
      bitDepth: 16,
      lastTriggerAt: null,
    },
    espNow: {
      rssi: -74,
      hopCount: 0,
      lastHeartbeatAt: ago(3200),
    },
    lastSeenAt: ago(3000),
    ipAddress: '192.168.4.104',
    firmwareVersion: '0.3.1',
    flags: ['POSITION_UNKNOWN', 'CLOCK_UNSETTLED'],
  },
];

export const GOD_NODE_LATLON = { lat: GOD_LAT, lon: GOD_LON };
