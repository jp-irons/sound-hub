"""Sound Hub — configuration constants.

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

# How often to restart the mDNS browser to force a fresh query burst.
#
# AsyncServiceBrowser otherwise relies on receiving multicast "Added"
# announcements as they happen, and python-zeroconf's own periodic re-query
# interval grows over a long-running browser's lifetime (RFC 6762 backoff —
# can stretch to tens of minutes). On Wi-Fi, multicast announcements
# (including the firmware's own startup + 2s-later re-announce — see
# EspMdnsManager) can simply get lost, so a node can sit undiscovered for a
# long time even though it's been advertising the whole while. Periodically
# tearing down and recreating the browser re-issues the same query burst that
# happens at hub startup — confirmed to reliably catch a node that a fresh
# restart picked up instantly. Cheap on a small home LAN.
MDNS_RESCAN_INTERVAL_S = 180.0

# --- Status polling ---
# Nodes serve over HTTPS (framework provides an HTTPS server) with what's
# almost certainly a self-signed cert — hence verify=False in the clients
# that talk to nodes. This is fine on a closed home LAN; revisit if/when
# nodes get proper certs.
NODE_SCHEME = "https"
STATUS_POLL_INTERVAL_S = 5.0
STATUS_TIMEOUT_S = 3.0

# --- Database ---
DB_PATH = "sound_hub.db"
