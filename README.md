# Sound Hub

Base station application for the acoustic bird localisation system — a network
of ESP32-S3 remote nodes (single MEMS mic, GPS PPS timing, GPS-averaged
location) deployed across a 3Ha subtropical rainforest property in Brisbane,
Australia. Nodes capture audio and timing data and feed it to
[BirdNET-Analyzer](https://github.com/birdnet-team/BirdNET-Analyzer) running
on a Windows workstation; Sound Hub is the management/visualisation layer that
sits alongside it.

Sound Hub:

- discovers Sound Capture Nodes (SCNs) on the LAN via mDNS
- polls known nodes for live status
- persists node identity in a local SQLite database
- serves a REST API (FastAPI) consumed by a React/Leaflet map UI showing node
  locations and live status

This repo covers the base station only. Remote node firmware lives in the
`sound-capture-node` project; TDOA solving lives in `bird-locator`.

## Stack

- **Backend** — Python, FastAPI, uvicorn, aiosqlite, zeroconf (mDNS), httpx
- **Frontend** — React + Vite, react-leaflet (map), plain CSS

## Prerequisites

- Python 3.11+ (a `venv` is already present in this repo for local dev)
- Node.js 18+ and npm

## Quick start (Windows)

Once dependencies are installed (see setup steps below), you can launch both
the backend and frontend with a double-click rather than juggling two
terminals:

- `start-all.bat` — opens the backend and frontend each in their own window
- `start-backend.bat` — backend only (activates `venv/`, runs uvicorn on `:8000`)
- `start-frontend.bat` — frontend only (`npm run dev` on `:5173`)

## Backend — setup and run

From the repo root:

```bash
# create/activate a virtual environment (skip if using the existing venv/)
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS/Linux

# install dependencies
pip install -r server/requirements.txt

# run the API (reload enabled for development)
uvicorn server.main:app --reload --port 8000
```

The backend listens on `http://192.168.101.220:8000` (see
`server/config.py` — `BASE_STATION_IP` / `BASE_STATION_PORT`). It exposes the
REST API under `/api` and allows CORS from the Vite dev server
(`http://localhost:5173`).

On startup it initialises the SQLite database (`sound_hub.db`), starts mDNS
discovery, and launches a background poller that periodically checks each
known node's status endpoint.

## Frontend — setup and run

From the repo root:

```bash
npm install
npm run dev
```

This starts the Vite dev server (default `http://localhost:5173`), which talks
to the backend API at `:8000`.

Other available scripts:

```bash
npm run build      # production build
npm run preview    # preview the production build locally
```

## Project layout

```
server/             FastAPI backend
  main.py           App entrypoint, lifespan (db init, discovery, poller)
  config.py         Network/discovery/polling configuration constants
  routes.py         REST API routes (mounted under /api)
  discovery.py      mDNS discovery of SCN nodes
  poller.py         Background status polling task
  registry.py       Node identity (persisted) + live status (in-memory)
  db.py             SQLite persistence
  models.py         Pydantic models / API view types
  status_mapper.py  Maps raw node status to UI-facing status
  requirements.txt  Backend Python dependencies

src/                React frontend (Vite)
  App.jsx           Root component
  main.jsx          Entry point
  components/       TopBar, MapView, NodeSidebar, NodeCard, NodeDetail, ErrorBoundary
```

## Notes

- `sound_hub.db` (SQLite) and the `venv/` directory are local runtime
  artefacts and are git-ignored — re-create them as needed (see Backend setup
  above).
- Network/discovery constants in `server/config.py` (base station IP, mDNS
  service type, hostname prefix) are subject to change as the node
  provisioning design firms up — see project notes for the rationale.
- `start-*.bat` launchers assume the existing `venv/` and an `npm install`
  already done; re-run the setup steps above first if either is missing.
