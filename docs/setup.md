# Setup Guide

## Overview

Sound Hub is a FastAPI backend + React SPA for managing acoustic sensor nodes, receiving audio, and running BirdNET detection.

**Architecture:**

```
ESP32-S3 nodes  ──LAN──►  nginx :80  ──►  uvicorn :8000 (FastAPI)
                                                │
                                           sound_hub.db (SQLite)

Browser  ──────────────►  nginx :80  (serves built SPA + proxies /api/)
Internet (optional) ────►  cloudflared  ──►  nginx :80
```

**Runtime artefacts** (created on first run, git-ignored):

- `sound_hub.db` — SQLite database; node identities, positions, detections, audit log. Back this up before significant changes. The schema migrates automatically on startup.
- `auth_secret.key` — JWT signing key. Deleting it invalidates all sessions and triggers generation of a new key.

**First-run account setup** applies to all installation types. When the UI is opened for the first time with no user accounts in the database, a setup screen appears. Enter a username and password (minimum 8 characters, confirmed twice) and click **Create account**. This endpoint returns 404 once the admin account exists, so it cannot be used again.

Alternatively, from the command line:

```bash
curl -X POST http://localhost:8000/api/auth/setup \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "yourpassword"}'
```

---

## Developer install — Windows

For running locally with hot-reload. No nginx or systemd.

### Prerequisites

- Python 3.12 — required; 3.13+ is incompatible with birdnetlib/tensorflow-cpu.
  Install from [python.org](https://www.python.org/downloads/release/python-3120/) and ensure it is on your PATH.
- Node.js 18 or later and npm
- Git

### 1. Clone

```bat
git clone https://github.com/jp-irons/sound-hub sound-hub
cd sound-hub
```

### 2. Python environment

```bat
python3.12 -m venv venv
venv\Scripts\activate
pip install -r server/requirements.txt
```

### 3. Frontend dependencies

```bat
npm install
```

### 4. Config

```bat
copy config\soundhub.conf.example config\soundhub.conf
```

Open `config/soundhub.conf` and set `BASE_STATION_IP` to this machine's LAN IP. The key settings:

| Setting | Default | Notes |
|---|---|---|
| `BASE_STATION_IP` | `192.168.101.220` | This machine's LAN IP |
| `BASE_STATION_PORT` | `8000` | Backend port |
| `NODE_LAN_SUBNET` | `192.168.101.0/24` | Must match your LAN subnet |

`BASE_STATION_IP` and `NODE_LAN_SUBNET` must be on the same subnet.

### 5. Run

Backend (terminal 1):

```bat
uvicorn server.main:app --reload --port 8000
```

Frontend (terminal 2):

```bat
npm run dev
```

The UI is available at `http://localhost:5173`. The FastAPI interactive docs are at `http://localhost:8000/docs`.

`start-all.bat` in the repo root opens both in separate windows with a single double-click (requires venv and npm install to have been run).

### Troubleshooting

**`ModuleNotFoundError` on backend start** — venv is not active, or `pip install` has not been run.

**"Could not reach the base station API"** — backend is not running, or is on a different port.

**Login fails after setup** — check uvicorn startup logs for database migration errors before deleting the database.

**Tokens expire after 8 hours** — intentional. Re-login via the UI, or increase `AUTH_TOKEN_EXPIRE_HOURS` in `config/soundhub.conf` and restart.

---

## Production install — Ubuntu

For a persistent deployment managed by systemd, with nginx serving the built SPA.

### Prerequisites

- Ubuntu 22.04 or later
- Git
- sudo access

### 1. Create a service user and install directory

Sound Hub runs under a dedicated system user. The deploy directory is `/opt/sound-hub`.

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin sound-hub
sudo mkdir /opt/sound-hub
sudo chown sound-hub:sound-hub /opt/sound-hub
```

To allow your login user (`jon`) to work in the directory without sudo, add them to the `sound-hub` group:

```bash
sudo usermod -aG sound-hub jon
sudo chmod -R g+w /opt/sound-hub
sudo chgrp -R sound-hub /opt/sound-hub
```

Log out and back in for the group membership to take effect.

Git will refuse to operate in a directory not owned by your user. Add a safe directory exception:

```bash
git config --global --add safe.directory /opt/sound-hub
```

### 2. Clone

```bash
git clone https://github.com/jp-irons/sound-hub /opt/sound-hub
cd /opt/sound-hub
```

### 3. Run setup.sh

`setup.sh` is idempotent — safe to re-run after failures or updates.

```bash
bash deploy/setup.sh
```

This script:

1. Installs system packages (`nginx`, `python3.12`, `nodejs`, `npm`, `ffmpeg`)
2. Creates a Python 3.12 venv and installs `server/requirements.txt`
3. Builds the React SPA (`npm ci && npm run build`)
4. Creates `config/soundhub.conf` from the example if not present
5. Installs and starts `soundhub.service` (uvicorn on `127.0.0.1:8000`)
6. Installs and starts nginx serving the SPA and proxying `/api/`

### 4. Configure

```bash
nano /opt/sound-hub/config/soundhub.conf
```

Set `BASE_STATION_IP` to this machine's LAN IP and verify `NODE_LAN_SUBNET` matches your network. Apply:

```bash
sudo systemctl restart soundhub
```

### 5. Verify

Check service status:

```bash
sudo systemctl status soundhub
sudo systemctl status nginx
```

From another device on the LAN, open `http://<server-IP>`. The login page should appear. Follow the first-run account setup flow.

Logs:

```bash
journalctl -u soundhub -f
```

### Updating after code changes

```bash
cd /opt/sound-hub
bash deploy/redeploy.sh
```

This pulls latest, runs `npm ci && npm run build`, and restarts `soundhub`.
It only runs `pip install -r server/requirements.txt` when that file actually
changed in the pulled commits, so routine updates skip the (usually
unnecessary) dependency install automatically. Use `--force-deps` to install
regardless, or `--skip-deps` to never install, even if the file changed.

Equivalent manual steps, if you'd rather not use the script:

```bash
cd /opt/sound-hub
git pull
npm ci && npm run build
venv/bin/pip install -r server/requirements.txt   # if dependencies changed
sudo systemctl restart soundhub
```

nginx does not need restarting for frontend-only changes.

---

## Internet exposure — Cloudflare Tunnel

Cloudflare Tunnel creates an outbound-only encrypted connection to Cloudflare's edge. No router port-forwarding or TLS certificates are required. Assumes Sound Hub is already running and reachable on `localhost:80` via nginx.

### Prerequisites

- A domain active on Cloudflare DNS (free plan is sufficient)

### Step 1 — Install cloudflared

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
    -o /tmp/cloudflared
sudo install /tmp/cloudflared /usr/local/bin/cloudflared
cloudflared --version
```

### Step 2 — Authenticate with Cloudflare

```bash
cloudflared tunnel login
```

Open the printed URL in a browser, log into your Cloudflare account, and select your domain. A certificate is saved to `~/.cloudflared/cert.pem`.

### Step 3 — Create the tunnel

```bash
cloudflared tunnel create sound-hub
```

Note the tunnel ID printed — you will need it in Step 5. Credentials are written to `~/.cloudflared/<tunnel-id>.json`.

### Step 4 — Move credentials to /etc/cloudflared

The credentials must be accessible to the cloudflared system service, which runs as root:

```bash
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/cert.pem /etc/cloudflared/
sudo cp ~/.cloudflared/<tunnel-id>.json /etc/cloudflared/
sudo chown -R root:root /etc/cloudflared
sudo chmod 600 /etc/cloudflared/*.json /etc/cloudflared/cert.pem
```

Clean up the copies from your home directory — they are no longer needed:

```bash
rm ~/.cloudflared/cert.pem
rm ~/.cloudflared/<tunnel-id>.json
rmdir ~/.cloudflared   # only if the directory is now empty
```

### Step 5 — Write the tunnel config

Create `/etc/cloudflared/config.yml`, substituting your tunnel ID and domain:

```yaml
tunnel: <tunnel-id>
credentials-file: /etc/cloudflared/<tunnel-id>.json

ingress:
  - hostname: soundhub.yourdomain.com
    service: http://localhost:80
  - service: http_status:404
```

The final catch-all entry is required by cloudflared.

### Step 6 — Create the DNS record

```bash
cloudflared tunnel route dns sound-hub soundhub.yourdomain.com
```

This creates a CNAME in Cloudflare DNS pointing your hostname to the tunnel.

### Step 7 — Install as a systemd service

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

### Verification

```bash
sudo systemctl status cloudflared
sudo journalctl -u cloudflared -n 50
```

Open `https://soundhub.yourdomain.com` — the login page should appear over HTTPS.

### Notes

- `/etc/cloudflared/config.yml` and the credentials JSON contain secrets specific to this deployment. They are not tracked in git.
- The tunnel only proxies traffic arriving via nginx on `localhost:80`. The uvicorn port (8000) is never directly exposed.
- LAN audio nodes push directly to nginx on the LAN IP and are unaffected by the tunnel.

---

## Optional — BirdNET tools

The `tools/` directory contains standalone scripts for live microphone detection. These have heavier ML dependencies kept in a separate venv.

```bash
cd tools
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

| Script | Purpose |
|---|---|
| `mic_to_birdnet.py` | Live mic → BirdNET → CSV (single run) |
| `mic_to_birdnet_watch.py` | Continuous watch mode |
| `clear_detections.py` | Wipe the detections table in `sound_hub.db` |

See the docstring in each script for full usage options (e.g. `--list-devices`, `--geo`, `--threshold`).
