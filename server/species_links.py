"""Resolves per-species reference links for the hub UI (see
SpeciesSummaryList.jsx) — a Wikipedia article for "what is this bird" and an
eBird species code for possible future use (Macaulay Library media, range
maps — not surfaced in the UI yet, but free to compute alongside the
Wikipedia lookup since it's a pure local dict lookup, no network call).

Design notes:
- Keyed on scientific_name, not common_name. BirdNET's label set uses
  American spelling exclusively (e.g. "Gray Fantail"; verified zero "Grey"
  entries in BirdNET_GLOBAL_6K_V2.4_Labels.txt), while Wikipedia's actual
  article titles often use British/Australian spelling for non-US species
  (e.g. "Grey fantail"). MediaWiki auto-follows a redirect from the BirdNET
  spelling IF an editor happened to create one — true for common species,
  not guaranteed for all ~6500. The scientific binomial sidesteps this
  entirely: species articles almost always have the binomial as a working
  redirect/canonical target regardless of which common name the title uses.
  common_name is tried only as a fallback if the scientific-name lookup
  comes back with no matching page.
- Resolution is meant to run once per species (see db.species_links table +
  maybe_resolve_new below) and be cached, not re-run per request — this
  module makes real network calls to the Wikipedia API.

data/ebird_taxonomy_codes.json is vendored from birdnet_analyzer 2.4.0's
eBird_taxonomy_codes_2024E.json (CC BY-NC-SA 4.0, same license as the
BirdNET label set it was built alongside). It is a flat, bidirectional dict:
"Scientific name_Common name" -> eBird 6-letter code, and the reverse
code -> name. Verified 2026-07-20: covers all 6522 species labels in
BirdNET_GLOBAL_6K_V2.4_Labels.txt with zero misses and zero collisions.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import db

log = logging.getLogger("sound_hub.species_links")

_EBIRD_CODES_PATH = Path(__file__).parent / "data" / "ebird_taxonomy_codes.json"

# Populated lazily by _ebird_codes() — None until first access. Named
# distinctly from the accessor function below: giving them the same name
# would have the function's `def` silently rebind this module global to the
# function object itself, breaking the cache on the very first call.
_ebird_codes_cache: dict[str, str] | None = None

# Wikipedia's API etiquette requires an identifying User-Agent with contact
# info: https://meta.wikimedia.org/wiki/User-Agent_policy
_WIKI_USER_AGENT = "sound-hub/1.0 (bird localisation project; jon@irons.ws)"
_WIKI_API = "https://en.wikipedia.org/w/api.php"
_WIKI_TIMEOUT_S = 10.0


def _ebird_codes() -> dict[str, str]:
    global _ebird_codes_cache
    if _ebird_codes_cache is None:
        with open(_EBIRD_CODES_PATH, encoding="utf-8") as f:
            _ebird_codes_cache = json.load(f)
    return _ebird_codes_cache


def resolve_ebird_code(common_name: str, scientific_name: str) -> str | None:
    """Look up the eBird 6-letter species code for a BirdNET label. Pure
    local dict lookup, no network — safe to call from any context.

    Returns None for BirdNET's non-avian classes (Dog, Engine, Gun, Noise,
    etc.) which have placeholder entries in the vendored JSON but aren't
    real eBird taxa, and for any species not found (shouldn't happen for a
    genuine BirdNET label, but the JSON is static and could in principle
    drift from a future BirdNET label update).
    """
    key = f"{scientific_name}_{common_name}"
    return _ebird_codes().get(key)


async def _wikipedia_canonical_url(client: httpx.AsyncClient, title: str) -> str | None:
    """Return the canonical (redirect-resolved) Wikipedia URL for a page
    title, or None if no matching page exists."""
    try:
        resp = await client.get(
            _WIKI_API,
            params={
                "action": "query",
                "titles": title,
                "redirects": "1",
                "format": "json",
                "formatversion": "2",
            },
            headers={"User-Agent": _WIKI_USER_AGENT},
            timeout=_WIKI_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        log.warning("Wikipedia lookup failed for %r: HTTP %s", title, exc.response.status_code)
        return None
    except httpx.TimeoutException:
        log.warning("Wikipedia lookup timed out for %r", title)
        return None
    except httpx.ConnectError as exc:
        log.warning("Wikipedia lookup connection error for %r: %s", title, exc)
        return None
    except httpx.HTTPError as exc:
        log.warning("Wikipedia lookup failed for %r: %s", title, exc)
        return None

    pages = data.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return None

    resolved_title = pages[0]["title"]
    return f"https://en.wikipedia.org/wiki/{resolved_title.replace(' ', '_')}"


async def resolve_wikipedia_url(common_name: str, scientific_name: str) -> str | None:
    """Resolve a Wikipedia article URL for a species. Tries the scientific
    (binomial) name first — stable across regional common-name spelling
    differences — then falls back to the common name if that comes back
    with no page."""
    async with httpx.AsyncClient() as client:
        url = await _wikipedia_canonical_url(client, scientific_name)
        if url:
            return url
        return await _wikipedia_canonical_url(client, common_name)


async def resolve_species_links(common_name: str, scientific_name: str) -> dict:
    """Resolve both link types for one species. Never raises — a failed
    Wikipedia resolution is recorded as status='failed' so
    list_species_missing_links() picks it up for retry via the "Resolve
    missing species links" admin action, rather than being silently lost."""
    ebird_code = resolve_ebird_code(common_name, scientific_name)
    try:
        wikipedia_url = await resolve_wikipedia_url(common_name, scientific_name)
    except Exception:
        log.exception("Unexpected error resolving Wikipedia link for %s / %s", common_name, scientific_name)
        wikipedia_url = None

    return {
        "species_key": scientific_name,
        "common_name": common_name,
        "ebird_code": ebird_code,
        "wikipedia_url": wikipedia_url,
        "status": "ok" if wikipedia_url else "failed",
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }


async def _resolve_and_store(common_name: str, scientific_name: str) -> None:
    result = await resolve_species_links(common_name, scientific_name)
    await db.upsert_species_link(**result)
    if result["status"] == "failed":
        log.info("species_links: failed to resolve %s (%s) — will retry via resolve-missing", common_name, scientific_name)


def maybe_resolve_new(detections: list[dict]) -> None:
    """Fire-and-forget: for each distinct (common_name, scientific_name) in
    a just-persisted batch of detections, schedule a resolution+cache pass
    for any species not already in species_links. Called right after
    db.insert_detections() from both routes.py call sites (audio push,
    manual WAV analyze).

    Deliberately synchronous (schedules tasks, doesn't await them) — a
    Wikipedia round-trip must never add latency to the audio-push hot path,
    same reasoning as the TDOA orchestration trigger it sits next to in
    routes.py. Existing rows (including 'failed' ones — those are retried
    only via the explicit resolve-missing admin action, not automatically
    on every subsequent detection) are left alone.
    """
    seen = {(d["common_name"], d["scientific_name"]) for d in detections}

    async def _schedule():
        existing = {row["species_key"] for row in await db.list_species_links()}
        for common_name, scientific_name in seen:
            if scientific_name in existing:
                continue
            asyncio.create_task(_resolve_and_store(common_name, scientific_name))

    asyncio.create_task(_schedule())


async def resolve_missing(*, max_species: int | None = None) -> dict:
    """Resolve every species present in `detections` that has no
    species_links row, or whose row is status='failed'. Backs the admin
    "Resolve missing species links" button — also serves as the one-time
    backfill for species detected before this feature existed, since those
    never went through maybe_resolve_new().

    Sequential, not concurrent: at a few dozen species even on a
    long-running deployment, there's no throughput need that would justify
    the added complexity, and it keeps Wikipedia API load polite by
    construction.
    """
    missing = await db.list_species_missing_links()
    if max_species is not None:
        missing = missing[:max_species]

    resolved = 0
    failed = 0
    for row in missing:
        result = await resolve_species_links(row["common_name"], row["scientific_name"])
        await db.upsert_species_link(**result)
        if result["status"] == "ok":
            resolved += 1
        else:
            failed += 1

    return {"resolved": resolved, "failed": failed, "total": len(missing)}
