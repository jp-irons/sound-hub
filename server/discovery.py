"""REMOVED 2026-07-12 — mDNS discovery retired in favour of DNS (.irons.net.au).

This module used to run a zeroconf/mDNS service browser to find SCN nodes on
the LAN. Nodes now reach the hub via self-registration (routes.py:
POST /api/nodes/register) or a manual add (POST /api/nodes/manual) — see
project memory `project-mdns-to-dns-migration` for the full history.

No code imports this module anymore (main.py's lifespan no longer calls
discovery.start()). This file is left as an empty stub rather than deleted
because the tools available in this session cannot delete files on this
mount (confirmed: `rm` fails with "Operation not permitted" even on a
brand-new throwaway file).

TODO(Jon): delete this file — `git rm server/discovery.py` — there is
nothing left in it to keep.
"""
