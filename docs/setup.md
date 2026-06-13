# Setup Guide

## Prerequisites

- Python 3.11 or later
- Node.js 18 or later and npm
- Git

## 1. Clone and enter the repo

```bash
git clone <repo-url> sound-hub
cd sound-hub
```

## 2. Backend — Python environment

Create a virtual environment:

```bash
python -m venv venv
```

Activate it:

```bash
# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r server/requirements.txt
```

Key packages: `fastapi`, `uvicorn`, `aiosqlite`, `zeroconf`, `httpx`,
`birdnetlib`, `python-jose[cryptography]`, `passlib[bcrypt]`.

## 3. Frontend — Node dependencies

```bash
npm install
```

## 4. Copy the example configs

Configuration files are not tracked in git — copy the examples before first run:

```bash
cp config/soundhub.conf.example config/soundhub.conf
cp config/nginx.conf.example config/nginx.conf   # only needed for production deployment
```

On Windows:
```bat
copy config\soundhub.conf.example config\soundhub.conf
copy config\nginx.conf.example config\nginx.conf   REM only needed for production deployment
```

## 5. Configuration

Open `config/soundhub.conf` and adjust the network settings for your LAN.

| Setting | Default | Notes |
|---|---|---|
| `BASE_STATION_IP` | `192.168.101.220` | This machine's reserved LAN IP — change to match your network (most home networks use `192.168.0.x` or `192.168.1.x`) |
| `BASE_STATION_PORT` | `8000` | Backend port — only change if 8000 is already in use |
| `NODE_LAN_SUBNET` | `192.168.101.0/24` | Must match your LAN subnet — change the `101` to match your `BASE_STATION_IP` |
| `AUTH_TOKEN_EXPIRE_HOURS` | `8` | JWT lifetime |

`BASE_STATION_IP` and `NODE_LAN_SUBNET` must be on the same subnet. For example,
if your router assigns addresses in `192.168.1.x`, set `BASE_STATION_IP` to your
machine's address (e.g. `192.168.1.10`) and `NODE_LAN_SUBNET` to `192.168.1.0/24`.

Auto-detection of the LAN IP is planned — for now this is a one-time manual step.

## 6. Run the backend

```bash
uvicorn server.main:app --reload --port 8000
```

On first run the backend will:

- Create `sound_hub.db` (SQLite database)
- Generate `auth_secret.key` (JWT signing key — keep this out of version control)
- Log a startup warning that no admin account exists yet

## 7. Run the frontend

In a separate terminal (with the venv active if needed):

```bash
npm run dev
```

The UI is available at `http://localhost:5173`.

On Windows, `start-all.bat` runs both services in separate windows with a
single double-click (assumes `venv/` exists and `npm install` has been run).

## 8. First-run: create your admin account

When you open the UI for the first time, a setup screen appears because no
user accounts exist. Enter a username and password (minimum 8 characters,
confirmed twice), then click **Create account**. You are logged in immediately.

This screen only appears once — once the admin account exists, the setup
endpoint returns 404 and the login screen is shown instead.

If you prefer to do this from the command line:

```bash
curl -X POST http://localhost:8000/api/auth/setup \
  -H "Content-Type: application/json" \
  -d '{"username": "jon", "password": "yourpassword"}'
```

## 9. Verify

With both services running and an account created:

- Open `http://localhost:5173` — you should see the map and node list
- The top bar shows your username and role
- The FastAPI interactive docs are at `http://localhost:8000/docs` — useful
  for testing API endpoints directly (use the **Authorize** button to supply
  your Bearer token)

## Runtime artefacts

`sound_hub.db` and `auth_secret.key` are created in the repo root (next to
`server/`) at runtime. Both are git-ignored. `sound_hub.db` is persistent data — node identities,
positions, detections, and the audit log all live here. Treat it accordingly;
back it up before any significant changes. The schema migrates automatically
on startup so you should never need to delete it.
Deleting `auth_secret.key` invalidates all existing tokens and triggers
generation of a new key; any signed-in sessions will be logged out.

## Troubleshooting

**Backend won't start — `ModuleNotFoundError`**
The venv is not active, or `pip install -r server/requirements.txt` has not
been run. Activate the venv and re-run the install.

**Frontend shows "Could not reach the base station API"**
The backend is not running, or is on a different port. Check that uvicorn
started without errors on `:8000`.

**Login fails immediately after setup**
The `users` table is added automatically by the migration logic in `db.py`
whenever the backend starts — an existing database is updated in place without
losing node data. If you suspect database corruption, check the uvicorn
startup logs for migration errors before reaching for a delete.

**Tokens expire after 8 hours**
This is intentional. Re-login via the UI. To extend the lifetime, increase
`AUTH_TOKEN_EXPIRE_HOURS` in `server/config.py` and restart the backend.

## 10. Tools — optional BirdNET utilities

The `tools/` directory contains standalone scripts for live microphone
detection and related tasks. These have heavier ML dependencies
(`birdnet-analyzer`, `sounddevice`) that are kept in a **separate venv**
to avoid polluting the main server environment.

On Windows, run the installer once:

```bat
tools\install-tools.bat
```

This creates `tools/venv/`, activates it, and installs `tools/requirements.txt`.

To run manually on any platform:

```bash
cd tools
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Available scripts:

| Script | Purpose |
|---|---|
| `mic_to_birdnet.py` | Live mic → BirdNET → CSV (single run) |
| `mic_to_birdnet_watch.py` | Continuous watch mode |
| `clear_detections.py` | Wipe the detections table in `sound_hub.db` |

The `.bat` launchers in `tools/` check for the venv and print a clear error
if it is missing (`Run tools\install-tools.bat first`) — so they are safe to
double-click; they will tell you if setup is needed. See the docstring in each
script for full usage options (e.g. `--list-devices`, `--geo`, `--threshold`).

The tools venv is git-ignored; recreate it any time by re-running the
installer or the manual steps above.

---

## Production deployment (WSL2 on Windows)

The `deploy/` folder contains scripts that install Sound Hub as a persistent
service on a Windows machine running WSL2 — the intended setup for the NUC
base station.  The result is nginx on port 80 serving the built SPA and
proxying `/api/` to uvicorn, with both managed by systemd inside WSL2.

### Prerequisites

- Windows 11 (22H2 or later) on the target machine
- The repo cloned on the Windows machine (for `windows-setup.ps1`)
- An always-on Windows user account (the service runs in that user's WSL session)

### Step 1 — Windows host setup

Open PowerShell **as Administrator** and run:

```powershell
cd C:\path\to\sound-hub
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser   # once only
.\deploy\windows-setup.ps1
```

This script:
- Installs WSL2 + Ubuntu if not already present (triggers a reboot — re-run the script after rebooting)
- Adds `networkingMode=mirrored` to `~/.wslconfig` so WSL2 ports appear directly on the Windows LAN interface
- Creates a Task Scheduler task that keeps the WSL2 instance running after the user logs on
- Opens Windows Firewall for TCP 80 (SPA + API) and TCP 8000 (node audio push)

After the script completes, apply the networking change:

```powershell
wsl --shutdown
```

Then reopen Ubuntu from the Start menu.

### Step 2 — WSL2 first-run

The first time Ubuntu opens it will ask for a Linux username and password.
Accept the suggested username or choose your own — either works.  The account
is automatically added to `sudoers`, which `setup.sh` requires.

### Step 3 — Clone and run setup

Inside Ubuntu:

```bash
git clone <repo-url> ~/sound-hub
bash ~/sound-hub/deploy/setup.sh
```

`setup.sh` is idempotent — safe to re-run after failures or updates.  It:
1. Installs system packages (`nginx`, `python3-venv`, `nodejs`, `npm`)
2. Creates a Python venv and installs `server/requirements.txt`
3. Builds the React SPA (`npm ci && npm run build`)
4. Creates `config/soundhub.conf` from the example (if not already present)
5. Installs and starts a systemd unit (`soundhub.service`) running uvicorn on `127.0.0.1:8000`
6. Installs and starts nginx, configured to serve the SPA and proxy `/api/`

### Step 4 — Configure

```bash
nano ~/sound-hub/config/soundhub.conf
```

Set `BASE_STATION_IP` to the NUC's LAN IP address and ensure `NODE_LAN_SUBNET`
matches your network (see the [Configuration](#5-configuration) section above).

Apply the change:

```bash
sudo systemctl restart soundhub
```

### Step 5 — Verify

From another device on the LAN, open `http://<NUC-IP>`.  The Sound Hub login
page should appear.  On first run, follow the admin account setup flow.

To check service status inside WSL2:

```bash
sudo systemctl status soundhub
sudo systemctl status nginx
```

Logs:

```bash
journalctl -u soundhub -f
```

### Updating after code changes

Pull the changes, rebuild the SPA, and restart the backend:

```bash
cd ~/sound-hub
git pull
npm ci && npm run build
venv/bin/pip install -r server/requirements.txt   # if dependencies changed
sudo systemctl restart soundhub
```

nginx does not need restarting for frontend-only changes (it serves static
files directly from `dist/`).

### Internet exposure

The LAN deployment uses plain HTTP on port 80.  To expose Sound Hub on the
internet with HTTPS, see `config/nginx.conf.example` — it is pre-configured
for certbot / Let's Encrypt.  Run certbot inside WSL2:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.example.com
```

Certbot will patch the nginx config with TLS certificates and set up
automatic renewal.
