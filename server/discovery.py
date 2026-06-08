"""mDNS discovery — watches the network for SCN nodes and registers them.

Design (see project memory `project-bird-basestation-plan`): nodes carry no
pre-baked base-station address. The base does all the work of finding nodes;
nodes are passive HTTP servers that advertise themselves over mDNS.

TODO once real hardware is reachable on the network:
  - Confirm the actual mDNS service type the firmware advertises under
    (this assumes standard `_http._tcp.local.`,  e.g. via the framework's
    HTTPS server). Adjust MDNS_SERVICE_TYPE in config.py if it differs.
  - Confirm the advertised hostname format matches MDNS_HOSTNAME_PREFIX
    (e.g. "soundcapture-ed5de4.local").
  - Consider a custom service type (e.g. `_scn._tcp.local.`) as a firmware
    addition — cleaner than hostname pattern matching, discussed but parked.
"""
import asyncio
import logging
import re

from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from . import config, registry

log = logging.getLogger("sound_hub.discovery")

_hostname_re = re.compile(rf"^{re.escape(config.MDNS_HOSTNAME_PREFIX)}", re.IGNORECASE)

_azc: AsyncZeroconf | None = None
_browser: AsyncServiceBrowser | None = None
_rescan_task: asyncio.Task | None = None


def _matches_scn_pattern(short_hostname: str) -> bool:
    return bool(_hostname_re.match(short_hostname))


async def _register_discovered(zeroconf, service_type: str, name: str) -> None:
    info = AsyncServiceInfo(service_type, name)
    if not await info.async_request(zeroconf, 3000):
        log.debug("Could not resolve mDNS service info for %s", name)
        return

    hostname = (info.server or name).rstrip(".")
    short_hostname = hostname.split(".")[0]

    if not _matches_scn_pattern(short_hostname):
        return  # not an SCN node — some other device on the LAN

    addresses = info.parsed_scoped_addresses()
    ip_address = addresses[0] if addresses else None

    # zeroconf often fires Added immediately followed by Updated for the same
    # record (and now the periodic rescan re-confirms already-known nodes too
    # — see _rescan_loop), so logging every resolution at `info` would be
    # noisy. Only announce at `info` when this is genuinely new information —
    # a node we haven't seen, or one that's moved to a different address.
    existing = await registry.get_node(short_hostname)
    if existing is None or existing["ip_address"] != ip_address:
        log.info("Discovered SCN node '%s' at %s", short_hostname, ip_address)
    else:
        log.debug("Re-confirmed mDNS presence for '%s' at %s", short_hostname, ip_address)

    await registry.upsert_node(
        node_id=short_hostname,
        hostname=short_hostname,
        ip_address=ip_address,
        discovery_method="mdns",
    )


def _on_state_change(zeroconf, service_type: str, name: str,
                      state_change: ServiceStateChange) -> None:
    if state_change is ServiceStateChange.Removed:
        # We don't immediately drop the node from the registry on an mDNS
        # "goodbye" — the poller will mark it unreachable if it stops
        # responding. Avoids registry churn from transient mDNS blips.
        log.debug("mDNS goodbye for %s (registry entry retained; poller will judge reachability)", name)
        return

    if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
        asyncio.ensure_future(_register_discovered(zeroconf, service_type, name))


def _start_browser() -> None:
    """(Re)create the service browser against the existing AsyncZeroconf.

    Constructing a fresh AsyncServiceBrowser sends out a new burst of PTR
    queries — the same burst that happens at hub startup. That's the
    mechanism _rescan_loop leans on to catch nodes whose mDNS announcements
    were missed (see MDNS_RESCAN_INTERVAL_S in config.py for the rationale).
    """
    global _browser
    _browser = AsyncServiceBrowser(
        _azc.zeroconf,
        config.MDNS_SERVICE_TYPE,
        handlers=[_on_state_change],
    )


async def _rescan_loop() -> None:
    """Periodically restart the browser to re-issue a fresh query burst.

    Multicast announcements (including the firmware's own startup +
    2-second-later re-announce) can simply get lost on Wi-Fi — restarting
    the hub is what reliably catches a node in that state, so we do the
    hub-restart-equivalent (tear down + recreate the browser) on a timer
    rather than waiting on python-zeroconf's own slow query backoff.
    """
    while True:
        await asyncio.sleep(config.MDNS_RESCAN_INTERVAL_S)
        try:
            log.debug("mDNS rescan — restarting browser to trigger a fresh query burst")
            if _browser is not None:
                await _browser.async_cancel()
            _start_browser()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("mDNS rescan failed — continuing with existing browser")


async def start() -> None:
    global _azc, _rescan_task
    _azc = AsyncZeroconf()
    _start_browser()
    _rescan_task = asyncio.create_task(_rescan_loop())
    log.info("mDNS discovery started — watching '%s' for hostnames matching '%s*' "
             "(rescanning every %.0fs)",
             config.MDNS_SERVICE_TYPE, config.MDNS_HOSTNAME_PREFIX, config.MDNS_RESCAN_INTERVAL_S)


async def stop() -> None:
    global _azc, _browser, _rescan_task
    if _rescan_task is not None:
        _rescan_task.cancel()
        _rescan_task = None
    if _browser is not None:
        await _browser.async_cancel()
        _browser = None
    if _azc is not None:
        await _azc.async_close()
        _azc = None
