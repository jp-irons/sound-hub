"""Background task — prunes old/excess files out of the pulled-audio directory.

Runs as an asyncio task for the lifetime of the app (see main.py lifespan),
the same pattern poller.py uses for trigger_events pruning. Deliberately
implemented in-app rather than as a cron job / systemd timer: this project
runs on both the Linux base station and (during dev) Windows, and cron isn't
available on the latter.

Every file under routes.AUDIO_DIR is a single TDOA pull segment — a ~3s clip
centered on a trigger onset, not a standalone useful recording. Past a
retention_hours window there's no analytic value left in the raw file (the
audio_events DB row that references it is retained indefinitely; only the
on-disk WAV is pruned). This was added 2026-07-17 after the audio
directory's unbounded growth (~600MB/day) filled the base station's disk and
took the whole app down with sqlite3.OperationalError: disk I/O error.

Two independent limits, both configurable at runtime via
GET/PUT /api/audio-cleanup-settings (db.audio_cleanup_settings, SettingsTab.jsx
"Audio Cleanup" section) rather than a soundhub.conf key or a hardcoded
constant — added 2026-07-17 after a fleet expansion made a fixed retention
window insufficient on its own (more nodes -> proportionally faster growth,
so a time-only cutoff would need re-tuning every time the fleet grows):

  - retention_hours: age-based cutoff, checked first.
  - max_size_bytes:  absolute cap, enforced after age-based pruning by
    deleting oldest-first until the directory is back under the cap. This is
    what keeps disk usage bounded even as node count grows, without needing
    retention_hours to be re-tuned by hand each time.
"""
import asyncio
import logging
import os
import time

from . import db, routes

log = logging.getLogger("sound_hub.audio_cleanup")

# How often to sweep the directory. Not configurable (only retention_hours
# and max_size_bytes are) — hourly is comfortably finer-grained than the 3h
# retention floor needs, matching poller.TRIGGER_EVENTS_PRUNE_INTERVAL_S's
# reasoning: day/hour-granularity retention doesn't need frequent checks,
# this just keeps steady-state sweeps small.
AUDIO_CLEANUP_INTERVAL_S = 3600.0


def _sweep_audio_dir(
    audio_dir: str, cutoff_epoch: float, max_size_bytes: int,
) -> tuple[int, int, int, int]:
    """Prune audio_dir in two passes: age-based, then size-cap (oldest
    first). Returns (age_deleted, age_freed_bytes, size_deleted,
    size_freed_bytes).

    Synchronous/blocking — call via asyncio.to_thread from the loop below,
    since this directory can hold tens of thousands of files and scanning it
    blocks the event loop otherwise. A single os.scandir pass collects
    path/mtime/size for every file so the size-cap pass (if needed) doesn't
    require a second directory walk. Best-effort per file: a file that
    vanishes or errors out mid-sweep (e.g. a concurrent write still in
    progress) is skipped rather than aborting the whole pass.
    """
    if not os.path.isdir(audio_dir):
        return 0, 0, 0, 0

    entries: list[tuple[str, float, int]] = []  # (path, mtime, size)
    with os.scandir(audio_dir) as it:
        for entry in it:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
                entries.append((entry.path, st.st_mtime, st.st_size))
            except OSError as exc:
                log.warning("audio cleanup: skipping %s (%s)", entry.path, exc)

    # Pass 1: age-based. Anything older than cutoff is deleted outright;
    # everything else survives into the size-cap pass below.
    age_deleted = 0
    age_freed = 0
    survivors: list[tuple[str, float, int]] = []
    for path, mtime, size in entries:
        if mtime < cutoff_epoch:
            try:
                os.remove(path)
                age_deleted += 1
                age_freed += size
            except OSError as exc:
                log.warning("audio cleanup: skipping %s (%s)", path, exc)
                survivors.append((path, mtime, size))
        else:
            survivors.append((path, mtime, size))

    # Pass 2: size-cap, oldest-first, only if still over budget after pass 1.
    size_deleted = 0
    size_freed = 0
    total = sum(size for _, _, size in survivors)
    if total > max_size_bytes:
        survivors.sort(key=lambda e: e[1])  # oldest mtime first
        for path, _mtime, size in survivors:
            if total <= max_size_bytes:
                break
            try:
                os.remove(path)
                total -= size
                size_deleted += 1
                size_freed += size
            except OSError as exc:
                log.warning("audio cleanup: skipping %s (%s)", path, exc)

    return age_deleted, age_freed, size_deleted, size_freed


async def run() -> None:
    log.info("Audio cleanup started — checking every %.0fs", AUDIO_CLEANUP_INTERVAL_S)
    while True:
        try:
            settings = await db.get_audio_cleanup_settings()
            retention_hours = settings["retention_hours"]
            max_size_bytes = settings["max_size_bytes"]
            cutoff_epoch = time.time() - retention_hours * 3600

            age_deleted, age_freed, size_deleted, size_freed = await asyncio.to_thread(
                _sweep_audio_dir, routes.AUDIO_DIR, cutoff_epoch, max_size_bytes,
            )
            if age_deleted:
                log.info(
                    "audio cleanup: age-based — removed %d file(s) older than "
                    "%.1fh (%.1f MB freed)",
                    age_deleted, retention_hours, age_freed / 1_048_576,
                )
            if size_deleted:
                log.info(
                    "audio cleanup: size-cap — removed %d oldest file(s) to "
                    "stay under %.2f GB (%.1f MB freed)",
                    size_deleted, max_size_bytes / 1_073_741_824, size_freed / 1_048_576,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Audio cleanup iteration failed — continuing")
        await asyncio.sleep(AUDIO_CLEANUP_INTERVAL_S)
