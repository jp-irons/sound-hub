# Sound Hub

Base station application for the acoustic bird localisation system — a network
of ESP32-S3 remote nodes deployed across a 3Ha subtropical rainforest property
in Brisbane, Australia.

Sound Hub discovers and manages Sound Capture Nodes (SCNs) on the LAN, collects
audio, feeds detections to BirdNET-Analyzer, and visualises node locations and
bird activity on a map.

Related projects: `sound-capture-node` (node firmware), `bird-locator` (TDOA solver).

## Stack

- **Backend** — Python 3.11+, FastAPI, uvicorn, aiosqlite, zeroconf, httpx
- **Frontend** — React 18, Vite, react-leaflet

## Quick start

```bash
# Backend (create venv once, then activate it)
python -m venv venv
venv\Scripts\activate        # Windows — or: source venv/bin/activate
pip install -r server/requirements.txt
uvicorn server.main:app --reload --port 8000

# Frontend (separate terminal)
npm install
npm run dev
```

On Windows, `start-all.bat` opens both in separate windows with one double-click.
`start-backend.bat` will use the venv if present, or fall back to system Python with a warning.

The UI is at `http://localhost:5173`. On first run you will be prompted to create
an admin account before the application loads.

See [docs/setup.md](docs/setup.md) for full installation and first-run instructions.

## Project layout

```
server/             FastAPI backend
  main.py           App entrypoint, lifespan (db init, discovery, poller)
  auth.py           JWT authentication, role dependencies, node IP trust
  soundhub.conf         Network, discovery, auth configuration
  routes.py         REST API (mounted under /api)
  db.py             SQLite persistence (nodes, positions, detections, audit log)
  models.py         Pydantic models
  discovery.py      mDNS discovery of SCN nodes
  poller.py         Background status polling
  registry.py       Node identity + live status
  status_mapper.py  Maps raw node status to UI view
  requirements.txt  Python dependencies

src/                React frontend (Vite)
  App.jsx           Root component, auth state machine
  auth.js           In-memory token store, apiFetch wrapper
  components/
    AuthOverlay.jsx Login and first-run setup screens
    TopBar.jsx      Status bar with user identity
    MapView.jsx     Leaflet map
    NodeSidebar.jsx Node list
    NodeDetail.jsx  Per-node detail and configuration
    NodeCard.jsx    Node summary card
    NodeConfigModal.jsx  Node config editor
    NodePositionModal.jsx  Position entry

config/
  soundhub.conf.example   Configuration template — copy to config/soundhub.conf and edit
  soundhub.conf           Your local config (git-ignored)
  nginx.conf.example  nginx reverse proxy template — copy to config/nginx.conf and edit
  nginx.conf          Your local nginx config (git-ignored)

docs/
  setup.md          Installation and first-run guide
```

## Runtime artefacts (git-ignored)

| File | Notes |
|---|---|
| `sound_hub.db` | SQLite database — re-created on first run |
| `auth_secret.key` | JWT signing key — auto-generated, never commit |
| `venv/` | Python virtual environment — create with `python -m venv venv` |
| `audio/` | Received WAV files from nodes |

## Deployment

Production runs in an Ubuntu VM (Hyper-V, hostname `sound-hub`) hosted on a
Windows 11 NUC — not WSL2, which was tried and dropped in favour of a clean
VM. See [Production install — Ubuntu](docs/setup.md#production-install-ubuntu)
in the setup guide for the systemd + nginx install steps. The `deploy/`
folder contains the scripts (`windows-setup.ps1`, `setup.sh`) and config
templates (`soundhub.service`, `nginx-lan.conf`) referenced there.
