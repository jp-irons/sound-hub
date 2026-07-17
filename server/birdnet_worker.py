"""BirdNET-Analyzer singleton for sound-hub.

Loads the Analyzer once at startup (expensive — model weights + TFLite runtime)
and exposes a thread-safe analyze_wav() function for route handlers.

All birdnetlib/TensorFlow console output is suppressed; startup noise from the
C++ TFLite runtime (written directly to the OS handle) cannot be intercepted
but occurs only once during init().
"""
import contextlib
import os
import threading
from datetime import date

# Populated by init() — None until the lifespan startup has completed.
_analyzer = None

# Serializes calls into the shared _analyzer/TFLite interpreter across
# threads. Recording.analyze() is not documented as safe for concurrent
# invocation. Previously this was serialized incidentally — every caller
# awaited its own HTTP response before returning, so only one analysis was
# ever in flight per node's blocked request. Since routes.py's audio_push()
# now dispatches ordinary (non-corroboration) analysis as a detached
# asyncio.create_task (2026-07-17, see project_soundhub_congestion notes),
# a multi-node trigger burst can genuinely schedule two analyses into the
# executor at once — this lock keeps that guarantee explicit instead of
# accidental. Only blocks the executor's worker thread, never the event
# loop, so it doesn't reintroduce the blocking-response problem being fixed.
_analyzer_lock = threading.Lock()


def init() -> None:
    """Load the BirdNET model.  Call once from the FastAPI lifespan startup
    (in a thread executor so the event loop is not blocked).
    """
    global _analyzer
    from birdnetlib.analyzer import Analyzer  # deferred — heavy import

    with open(os.devnull, "w") as devnull:
        with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
            _analyzer = Analyzer()


def ready() -> bool:
    return _analyzer is not None


DEFAULT_MIN_CONF = 0.5


def _analyze(path: str, *, use_geo: bool, min_conf: float) -> list[dict]:
    """Shared implementation — analyse a WAV at the given min_conf cutoff."""
    if _analyzer is None:
        raise RuntimeError("BirdNET worker not initialised — call init() first")

    from birdnetlib import Recording  # deferred

    kwargs: dict = {"min_conf": min_conf}
    if use_geo:
        today = date.today()
        kwargs.update(lat=-27.5, lon=153.0, date=today)

    with _analyzer_lock:
        recording = Recording(_analyzer, path, **kwargs)
        with open(os.devnull, "w") as devnull:
            with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
                recording.analyze()

    return recording.detections or []


def analyze_wav(
    path: str,
    *,
    use_geo: bool = False,
    min_conf: float = DEFAULT_MIN_CONF,
) -> list[dict]:
    """Analyse a WAV file and return a list of detection dicts.

    Each dict contains:
        common_name, scientific_name, start_time, end_time, confidence

    Raises RuntimeError if called before init().
    """
    return _analyze(path, use_geo=use_geo, min_conf=min_conf)


def analyze_wav_full(path: str, *, use_geo: bool = False) -> list[dict]:
    """Analyse a WAV file and return EVERY candidate BirdNET considered,
    regardless of confidence — i.e. min_conf=0.0.

    Used for diagnostics (audio_events.top_confidence/top_species): lets a
    caller see the best candidate even when it falls below
    DEFAULT_MIN_CONF and would otherwise never be persisted anywhere.
    Callers still apply their own threshold before persisting to the
    `detections` table — this function does not replace that filtering.

    Raises RuntimeError if called before init().
    """
    return _analyze(path, use_geo=use_geo, min_conf=0.0)
