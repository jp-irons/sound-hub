"""Background task — prunes old files out of the pulled-audio directory.

Runs as an asyncio task for the lifetime of the app (see main.py lifespan),
the same pattern poller.py uses for trigger_events pruning. Deliberately
implemented in-app rather than as a cron job / systemd timer: this project
runs on both the Linux base station and (during dev) Windows, and cron isn't
available on the latter.

Every file under routes.AUDIO_DIR is a single TDOA pull segment — a ~3s clip
centered on a trigger onset, not a standalone useful recording. Past a
couple of days there's no analytic value left in the raw file (the
audio_events DB row that references it is retained indefinitely; only the
on-disk WAV is pruned), so a short retention window is safe. This was added
2026-07-17 after the audio directory's unbounded growth (~600MB/day) filled
the base station's disk and took the whole app down with
sqlite3.OperationalError: disk I/O error.
"""
import asyncio
import logging
import os
import time

from . import routes

log = logging.getLogger("sound_hub.audio_cleanup")

# Files older than this are deleted. Deliberately a hardcoded constant
# rather than a soundhub.conf key — same reasoning as
# db.TRIGGER_EVENTS_RETENTION_HOURS: avoids a footgun where an existing
# deployment's soundhub.conf doesn't define it and cleanup silently never
# runs. 48h (2 days) is ample time to review or download an interesting
# capture via the UI before it's pruned.
AUDIO_RETENTION_HOURS = 48

# How often to sweep the directory. Hourly is comfortably finer-grained
# than the 48h retention window needs — matches
# poller.TRIGGER_EVENTS_PRUNE_INTERVAL_S's reasoning (day-granularity
# retention doesn't need frequent checks; this just keeps steady-state
# sweeps small).
AUDIO_CLEANUP_INTERVAL_S = 3600.0


def _prune_old_audio_files(audio_dir: str, cutoff_epoch: float) -> tuple[int, int]:
    """Delete files in audio_dir with mtime older than cutoff_epoch.

    Returns (files_deleted, bytes_freed). Synchronous/blocking — call via
    asyncio.to_thread from the async loop below, since this directory can
    hold tens of thousands of files and scanning it blocks the event loop
    otherwise. Best-effort per file: a file that vanishes or errors out
    mid-sweep (e.g. a concurrent write still in progress) is skipped rather
    than aborting the whole pass.
    """
    deleted = 0
    freed_bytes = 0
    if not os.path.isdir(audio_dir):
        return deleted, freed_bytes

    with os.scandir(audio_dir) as it:
        for entry in it:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
                if st.st_mtime < cutoff_epoch:
                    os.remove(entry.path)
                    deleted += 1
                    freed_bytes += st.st_size
            except OSError as exc:
                log.warning("audio cleanup: skipping %s (%s)", entry.path, exc)
                continue

    return deleted, freed_bytes


async def run() -> None:
    log.info(
        "Audio cleanup started — retention %.0fh, checking every %.0fs",
        AUDIO_RETENTION_HOURS, AUDIO_CLEANUP_INTERVAL_S,
    )
    while True:
        try:
            cutoff_epoch = time.time() - AUDIO_RETENTION_HOURS * 3600
            deleted, freed_bytes = await asyncio.to_thread(
                _prune_old_audio_files, routes.AUDIO_DIR, cutoff_epoch,
            )
            if deleted:
                log.info(
                    "audio cleanup: removed %d file(s) older than %dh (%.1f MB freed)",
                    deleted, AUDIO_RETENTION_HOURS, freed_bytes / 1_048_576,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Audio cleanup iteration failed — continuing")
        await asyncio.sleep(AUDIO_CLEANUP_INTERVAL_S)
