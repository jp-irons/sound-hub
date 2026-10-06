"""Background task — SENTINEL-MODE-PLAN.md Track B3.

Continuously pulls audio from every hub-side `sentinel`-flagged node (Track
B2's bookkeeping-only flag — see registry.set_sentinel) and runs BirdNET on
every window pulled, via the same pipeline a triggered push uses
(_analyze_and_persist_detection, Track B1). This is deliberately a separate
task from poller.run() — different cadence, different purpose (this one
retrieves and analyzes actual audio; poller.py only fetches status JSON) —
registered alongside it in main.py's lifespan.

Per-node queueing (2026-10-06, replaces the original skip-only guard): a
sentinel node is polled on a fixed tick regardless of whether its previous
pull finished (see run() below — ticks are dispatched via
asyncio.create_task, never awaited inline), so a slow pull must not be
allowed to start a second, overlapping pull to the same node. Originally a
tick that found its node still busy just skipped outright — simple, but it
meant a single slow pull (see 2026-10-06 diagnosis below) permanently lost
that window's audio coverage even though most such slowdowns are brief and
the node is free again well within a tick or two.

Now a busy node's tick is queued instead of dropped: _queues holds, per
node, the windows still waiting their turn (oldest first). _node_worker
drains it — pulling+analyzing the window it was started with, then
whatever is next in the queue, back to back, until the queue is empty, then
exits. Only one worker runs per node at a time (_in_flight now means "a
worker is active for this node", not "a pull is in flight" — the worker may
be between pulls, about to start its next queued one). A bounded queue
(SENTINEL_QUEUE_MAXLEN) keeps a sustained slowdown from building an
unbounded backlog of increasingly stale windows: once full, a newly-ticked
window bumps the OLDEST queued one out rather than being rejected itself —
newer coverage is worth more than older — and that eviction is what now
counts as the overlap "skip", via registry.record_sentinel_overlap_skip, so
it's still visible outside the logs (see that function's docstring and
NodeView.sentinel_overlap_skips in models.py / the "Sentinel" kv row in
NodeDetail.jsx). A queued (delayed) window pulled somewhat late than its
tick time can still come back 404 "window unavailable" if the node's own
ring buffer has since evicted it — already handled the same way an
ordinary pull's 404 always has been (see _pull_and_analyze_one), no new
failure mode.

2026-10-06 diagnosis note: timing instrumentation (both here and in
routes._fetch_audio_direct's purpose= tagging/contention check) ruled out
hub-side causes for the pull-time variance (0.6-3.3s, occasionally enough to
trip the old skip-only guard) prompting this — no TDOA corroboration pulls
were in flight, no other sentinel node was active, and the node's own
reported Wi-Fi signal was strong (-50 dBm). The actual mechanism was left
unresolved; this change doesn't depend on knowing it — it just stops an
occasional, apparently node/network-side slow pull from costing a
permanently lost window when the node is free again moments later.
"""
import asyncio
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone

from fastapi import HTTPException

from . import birdnet_worker, db, registry, routes

log = logging.getLogger("sound_hub.sentinel_poller")

# Window length pulled on every tick, and how often ticks fire. Previously
# 3.5s (0.5s longer than the tick interval) on the theory that the extra
# margin gave a call landing near a window boundary a second chance to land
# whole in the next pull. That rationale turned out not to hold: birdnetlib
# (RecordingBase.process_audio_data) hardcodes a 3.0s chunk and silently
# drops any trailing remainder under 1.5s, so the "extra" 0.5s was never
# reaching the model at all — every 3.5s pull was only ever analyzed as its
# first 3.0s (confirmed 2026-10-06 by reading birdnetlib's installed
# source). Dropped to 3.0s to match what's actually analyzed; real-time
# coverage still tiles contiguously tick to tick since consecutive windows
# abut exactly. SENTINEL_TRAILING_MARGIN_S keeps the requested window a
# little behind the node's actual "now" so the pull never races audio the
# node hasn't finished writing into its ring yet.
SENTINEL_WINDOW_S = 3.0
SENTINEL_TICK_INTERVAL_S = 3.0
SENTINEL_TRAILING_MARGIN_S = 1.0

# Per-node cap on queued-but-not-yet-pulled windows (not counting whichever
# one the worker is currently pulling). At the observed pull durations
# (mostly well under 3s, occasionally 2-3.3s), 2 absorbs a single slow cycle
# or two without ever dropping anything — i.e. the common case in the
# 2026-10-06 trace fully recovers rather than losing coverage. A sustained
# slowdown beyond that starts evicting oldest-first rather than growing an
# unbounded, increasingly-stale backlog. Tune directly if field data says
# otherwise.
SENTINEL_QUEUE_MAXLEN = 2

# node_id -> True while a worker (pulling, analyzing, or about to start its
# next queued window) is active for it. See module docstring.
_in_flight: set[str] = set()

# node_id -> windows waiting for the active worker to get to them, oldest
# first. Only ever non-empty for a node that's also in _in_flight. Each
# entry carries the time.monotonic() timestamp it was created at (tick
# time, whether dispatched immediately or queued) — see _node_worker's
# "sentinel dispatch" log line, added 2026-10-06 because the pull/analyze
# timing alone can't tell a window that waited in queue before its request
# was even sent from one that was slow for other reasons once sent; Jon's
# point reading the 15:54-15:55 trace.
_queues: dict[str, deque[tuple[int, int, float]]] = {}


def _current_window_us() -> tuple[int, int]:
    now_us = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
    end_us = now_us - int(SENTINEL_TRAILING_MARGIN_S * 1_000_000)
    start_us = end_us - int(SENTINEL_WINDOW_S * 1_000_000)
    return start_us, end_us


async def _pull_and_analyze_one(node_id: str, t_start_us: int, t_end_us: int) -> None:
    """Pull and analyze exactly one window for node_id.

    No overlap guard here — that's _node_worker's job (only one of these
    ever runs at a time per node_id, enforced by _in_flight/_queues below).
    Self-contained: every exception is caught here, same as every other
    fire-and-forget create_task callee in this codebase (poller._poll_one,
    routes._correlate_and_maybe_solve, etc.) — _node_worker must never see
    this raise.
    """
    # Timing instrumentation (2026-10-06) — tells apart the pull leg
    # (network-bound, waiting on the node) from the analyze leg (CPU-bound
    # BirdNET inference, including any time spent waiting on
    # _analysis_semaphore if something else is analyzing concurrently).
    # Logged at INFO so it shows up in the normal journalctl -u soundhub
    # stream without needing debug level.
    pull_started = time.monotonic()
    try:
        wav_bytes, actual_start_us, actual_end_us, _noise_floor_rms = (
            await routes._fetch_audio_direct(
                node_id, t_start_us, t_end_us, purpose="sentinel",
            )
        )
    except HTTPException as exc:
        pull_elapsed = time.monotonic() - pull_started
        # 404/503 are the node's own answer (window unavailable / not
        # capturing right now) — ordinary and frequent at this cadence
        # (and, now that a delayed queued pull can legitimately ask for a
        # window the node's ring has since evicted, expected occasionally
        # even without anything being wrong) — not worth more than debug.
        # 424/502/504 mean the request never got a real answer at all —
        # same partition _fetch_audio_direct documents for the TDOA-pull
        # caller.
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


async def _still_sentinel(node_id: str) -> bool:
    """True if node_id is still an approved sentinel node right now.

    Checked by _node_worker before draining each queued window (not just
    once at tick time) — an operator can flip sentinel off mid-backlog, and
    without this the worker would keep burning queued pulls against a node
    that's no longer supposed to be polled at all until the backlog drains
    on its own.
    """
    node = await registry.get_node(node_id)
    return node is not None and node["approval_status"] == db.APPROVED and node["sentinel"]


async def _node_worker(node_id: str, t_start_us: int, t_end_us: int, created_at: float) -> None:
    """Pulls+analyzes node_id's windows back to back — the one it's started
    with, then whatever _queues[node_id] has waiting by the time each one
    finishes — until the queue is empty (or sentinel gets turned off for
    this node mid-backlog), then exits.

    Exactly one of these runs per node_id at a time; run() below enqueues
    onto _queues instead of starting a second one while this is active —
    see _in_flight's docstring.

    created_at (time.monotonic(), added 2026-10-06) is when run() decided
    to send this particular window — at tick time, whether it got dispatched
    immediately or sat in _queues for a while first. Logged right before
    the request actually goes out (see "sentinel dispatch" below) so a slow
    *pull* (time once sent) and a slow *dispatch* (time waiting its turn
    before being sent at all) show up as two distinct, attributable numbers
    instead of being folded into one.
    """
    try:
        while True:
            queue_wait_s = time.monotonic() - created_at
            log.info(
                "sentinel dispatch %s — sending request (queued %.2fs before send)",
                node_id, queue_wait_s,
            )
            await _pull_and_analyze_one(node_id, t_start_us, t_end_us)
            queue = _queues.get(node_id)
            if not queue:
                break
            if not await _still_sentinel(node_id):
                log.info(
                    "sentinel worker for %s stopping — no longer a sentinel "
                    "node, dropping %d queued window(s)",
                    node_id, len(queue),
                )
                break
            t_start_us, t_end_us, created_at = queue.popleft()
    finally:
        _in_flight.discard(node_id)
        _queues.pop(node_id, None)


async def run() -> None:
    log.info(
        "Sentinel poller started — %.1fs window every %.1fs, queue depth %d",
        SENTINEL_WINDOW_S, SENTINEL_TICK_INTERVAL_S, SENTINEL_QUEUE_MAXLEN,
    )
    while True:
        try:
            # Mirrors audio_push()'s own not-ready guard: BirdNET loads in a
            # thread during startup (see main.py lifespan) and this task is
            # started before that finishes, so early ticks have nothing
            # useful to do yet. Checked once per tick rather than inside
            # _pull_and_analyze_one — no point pulling a WAV at all if it
            # can't be analyzed.
            nodes = [n for n in await registry.list_nodes()
                     if n["approval_status"] == db.APPROVED and n["sentinel"]]
            if nodes and not birdnet_worker.ready():
                log.debug("sentinel tick skipped — BirdNET not yet loaded")
                nodes = []
            if nodes:
                t_start_us, t_end_us = _current_window_us()
                # Fire-and-forget per node, NOT gather-and-wait: ticks must
                # keep firing on the fixed interval regardless of whether a
                # given node's previous pull has finished — that
                # independence is the only way _in_flight/_queues above can
                # ever have something to guard/queue against. A slow/stuck
                # node delays only itself, never the other sentinels'
                # cadence.
                for node in nodes:
                    node_id = node["id"]
                    created_at = time.monotonic()
                    if node_id not in _in_flight:
                        _in_flight.add(node_id)
                        asyncio.create_task(
                            _node_worker(node_id, t_start_us, t_end_us, created_at)
                        )
                        continue

                    queue = _queues.setdefault(node_id, deque())
                    queue.append((t_start_us, t_end_us, created_at))
                    if len(queue) > SENTINEL_QUEUE_MAXLEN:
                        queue.popleft()  # oldest — newer coverage wins
                        skip_count = registry.record_sentinel_overlap_skip(node_id)
                        log.warning(
                            "sentinel queue full for %s (max %d) — dropped "
                            "oldest queued window (skip #%d for this node)",
                            node_id, SENTINEL_QUEUE_MAXLEN, skip_count,
                        )
                    else:
                        log.debug(
                            "sentinel tick queued for %s — worker busy "
                            "(queue depth %d)", node_id, len(queue),
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Sentinel poller tick failed — continuing")
        await asyncio.sleep(SENTINEL_TICK_INTERVAL_S)
