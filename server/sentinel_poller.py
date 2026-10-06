"""Background task — SENTINEL-MODE-PLAN.md Track B3.

Continuously pulls audio from every hub-side `sentinel`-flagged node (Track
B2's bookkeeping-only flag — see registry.set_sentinel) and runs BirdNET on
every window pulled, via the same pipeline a triggered push uses
(_analyze_and_persist_detection, Track B1). This is deliberately a separate
task from poller.run() — different cadence, different purpose (this one
retrieves and analyzes actual audio; poller.py only fetches status JSON) —
registered alongside it in main.py's lifespan.

Self-overlap guard: a sentinel node is polled on a fixed tick regardless of
whether its previous pull finished (see run() below — ticks are dispatched
via asyncio.create_task, never awaited inline), so a slow pull must not be
allowed to start a second, overlapping pull to the same node. _in_flight
tracks this per node_id. When a tick finds its node already in flight, it
skips rather than queuing — the next tick will try again — and records the
skip via registry.record_sentinel_overlap_skip so it's visible outside the
logs too (see that function's docstring and NodeView.sentinel_overlap_skips
in models.py / the "Sentinel" kv row in NodeDetail.jsx).
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from fastapi import HTTPException

from . import birdnet_worker, db, registry, routes

log = logging.getLogger("sound_hub.sentinel_poller")

# Window length pulled on every tick, and how often ticks fire. 3.5s every
# 3s means each window overlaps the previous one by half a second —
# deliberate margin so a call landing near a window boundary still lands
# whole in at least one pull, rather than being split across two and missed
# by both. SENTINEL_TRAILING_MARGIN_S keeps the requested window a little
# behind the node's actual "now" so the pull never races audio the node
# hasn't finished writing into its ring yet.
SENTINEL_WINDOW_S = 3.5
SENTINEL_TICK_INTERVAL_S = 3.0
SENTINEL_TRAILING_MARGIN_S = 1.0

# node_id -> in-flight pull/analyze pipeline currently running for it. See
# module docstring's "self-overlap guard" paragraph.
_in_flight: set[str] = set()


def _current_window_us() -> tuple[int, int]:
    now_us = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
    end_us = now_us - int(SENTINEL_TRAILING_MARGIN_S * 1_000_000)
    start_us = end_us - int(SENTINEL_WINDOW_S * 1_000_000)
    return start_us, end_us


async def _pull_and_analyze(node_id: str, t_start_us: int, t_end_us: int) -> None:
    """One sentinel pull-and-analyze pipeline for node_id, guarded against
    overlapping a still-running previous pull for the same node.

    Self-contained: every exception is caught here, same as every other
    fire-and-forget create_task callee in this codebase (poller._poll_one,
    routes._correlate_and_maybe_solve, etc.) — run()'s tick loop must never
    see one of these tasks raise.
    """
    if node_id in _in_flight:
        skip_count = registry.record_sentinel_overlap_skip(node_id)
        log.warning(
            "sentinel poll skipped for %s — previous pull still in flight "
            "(skip #%d for this node)", node_id, skip_count,
        )
        return

    _in_flight.add(node_id)
    # Timing instrumentation (2026-10-06) — added to tell apart two very
    # different possible causes of the overlap guard firing: the pull leg
    # (network-bound, waiting on the node — near-zero hub CPU while it
    # waits) vs. the analyze leg (CPU-bound BirdNET inference, including
    # any time spent waiting on _analysis_semaphore if something else is
    # analyzing concurrently). Logged at INFO so it shows up in the normal
    # journalctl -u soundhub stream without needing debug level.
    pull_started = time.monotonic()
    try:
        try:
            wav_bytes, actual_start_us, actual_end_us, _noise_floor_rms = (
                await routes._fetch_audio_direct(
                    node_id, t_start_us, t_end_us, purpose="sentinel",
                )
            )
        except HTTPException as exc:
            pull_elapsed = time.monotonic() - pull_started
            # 404/503 are the node's own answer (window unavailable / not
            # capturing right now) — ordinary and frequent at this cadence,
            # not worth more than debug. 424/502/504 mean the request never
            # got a real answer at all — same partition _fetch_audio_direct
            # documents for the TDOA-pull caller.
            if exc.status_code in (404, 503):
                log.debug("sentinel pull %s — node said no (%s) after %.2fs: %s",
                          node_id, exc.status_code, pull_elapsed, exc.detail)
            else:
                log.debug("sentinel pull %s — request failed (%s) after %.2fs: %s",
                          node_id, exc.status_code, pull_elapsed, exc.detail)
            return
        except Exception:
            log.exception("sentinel pull %s — unexpected failure after %.2fs",
                           node_id, time.monotonic() - pull_started)
            return

        pull_elapsed = time.monotonic() - pull_started
        log.info("sentinel pull %s — %.2fs, %d bytes",
                  node_id, pull_elapsed, len(wav_bytes))

        os.makedirs(routes._AUDIO_DIR, exist_ok=True)
        fname = f"sentinel_{node_id}_{actual_start_us}.wav"
        fpath = os.path.join(routes._AUDIO_DIR, fname)
        with open(fpath, "wb") as fh:
            fh.write(wav_bytes)

        # NOT _save_direct_pull_audio — that helper deliberately skips
        # BirdNET (a TDOA-corroboration pull already knows the species from
        # the origin detection). A sentinel pull has no such foreknowledge;
        # every window must actually be analyzed, so this goes through
        # Track B1's shared pipeline instead (triggered=False: nothing on
        # this node's own AudioTrigger fired, the hub just asked).
        analyze_started = time.monotonic()
        await routes._analyze_and_persist_detection(
            node_id=node_id, fname=fname, fpath=fpath, bytes_len=len(wav_bytes),
            t_start_us=actual_start_us, t_end_us=actual_end_us, triggered=False,
        )
        analyze_elapsed = time.monotonic() - analyze_started
        log.info(
            "sentinel analyze %s — %.2fs (pull %.2fs, total %.2fs)",
            node_id, analyze_elapsed, pull_elapsed, time.monotonic() - pull_started,
        )
    finally:
        _in_flight.discard(node_id)


async def run() -> None:
    log.info(
        "Sentinel poller started — %.1fs window every %.1fs",
        SENTINEL_WINDOW_S, SENTINEL_TICK_INTERVAL_S,
    )
    while True:
        try:
            # Mirrors audio_push()'s own not-ready guard: BirdNET loads in a
            # thread during startup (see main.py lifespan) and this task is
            # started before that finishes, so early ticks have nothing
            # useful to do yet. Checked once per tick rather than inside
            # _pull_and_analyze — no point pulling a WAV at all if it can't
            # be analyzed.
            nodes = [n for n in await registry.list_nodes()
                     if n["approval_status"] == db.APPROVED and n["sentinel"]]
            if nodes and not birdnet_worker.ready():
                log.debug("sentinel tick skipped — BirdNET not yet loaded")
                nodes = []
            if nodes:
                t_start_us, t_end_us = _current_window_us()
                # Fire-and-forget per node, NOT gather-and-wait: ticks must
                # keep firing on the fixed interval regardless of whether a
                # given node's previous pull has finished — that independence
                # is the only way _in_flight above can ever actually have
                # something to guard against. A slow/stuck node delays only
                # itself, never the other sentinels' cadence.
                for node in nodes:
                    asyncio.create_task(
                        _pull_and_analyze(node["id"], t_start_us, t_end_us)
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Sentinel poller tick failed — continuing")
        await asyncio.sleep(SENTINEL_TICK_INTERVAL_S)
