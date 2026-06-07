"""Acoustic Base Station — configuration constants.

Adjust these as the network/provisioning design firms up. See the
project memory `project-bird-basestation-plan` for the rationale
behind the network and discovery decisions recorded here.
"""

# This machine's reserved LAN address — pushed to nodes during provisioning
# so they know where to send audio/status. DHCP-reserved; see memory for why
# `.local` hostname resolution was ruled out (Windows resolves to non-routable
# link-local IPv6 addresses between machines).
BASE_STATION_IP = "192.168.101.220"
BASE_STATION_PORT = 8000

# --- mDNS discovery ---
# SCN nodes advertise over standard _http._tcp; we filter by hostname prefix.
# TODO: confirm this matches the actual mDNS service type/hostname the firmware
# advertises (e.g. "soundcapture-ed5de4.local") — adjust prefix/service type
# once we can observe a real node on the network.
MDNS_SERVICE_TYPE = "_http._tcp.local."
MDNS_HOSTNAME_PREFIX = "soundcapture-"

# --- Status polling ---
# Nodes serve over HTTPS (framework provides an HTTPS server) with what's
# almost certainly a self-signed cert — hence verify=False in the clients
# that talk to nodes. This is fine on a closed home LAN; revisit if/when
# nodes get proper certs.
NODE_SCHEME = "https"
STATUS_POLL_INTERVAL_S = 5.0
STATUS_TIMEOUT_S = 3.0

# --- Database ---
DB_PATH = "acoustic_base.db"
