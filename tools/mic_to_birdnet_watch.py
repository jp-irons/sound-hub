"""
mic_to_birdnet_watch.py — Live mic → BirdNET with watch-list and daily file rotation

Two-tier detection handling:
  • Watched species  → incident log CSV + WAV saved to dated subfolder
  • Common species   → running count in daily summary CSV

File layout (all relative to --root, default: ./birdwatch):

    birdwatch/
        detections/
            summary_2026-06-11.csv      ← all-species counts, rewritten each update
            incidents_2026-06-11.csv    ← one row per watched-species detection
        samples/
            2026-06-11/
                Barn Owl/
                    mic_2026-06-11_213412.wav
                Bush Stone-curlew/
                    mic_2026-06-11_221507.wav

Species watch list: species_config.yaml (same folder as this script, or --species-config).

Usage:
    python tools/mic_to_birdnet_watch.py --device "ReSpeaker" --geo
    python tools/mic_to_birdnet_watch.py --list-devices
"""

import argparse
import contextlib
import csv
import os
import tempfile
from datetime import datetime, date
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
from birdnetlib import Recording
from birdnetlib.analyzer import Analyzer

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


# ── Constants ─────────────────────────────────────────────────────────────────

SAMPLE_RATE    = 48_000
CHUNK_SECONDS  = 3
BRISBANE_LAT   = -27.5
BRISBANE_LON   = 153.0

SUMMARY_FIELDS  = ["common_name", "scientific_name", "count", "first_seen", "last_seen"]
INCIDENT_FIELDS = ["timestamp", "common_name", "scientific_name", "confidence", "wav_file"]


# ── Species config ────────────────────────────────────────────────────────────

def load_watch_list(config_path: str) -> set[str]:
    """Return a set of lower-cased common names that should be watched."""
    if not os.path.exists(config_path):
        print(f"  Warning: species config not found at '{config_path}' "
              "— all species treated as common.")
        return set()

    if _HAS_YAML:
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        names = cfg.get("watch", [])
    else:
        # Minimal fallback parser for "  - Name" lines under a "watch:" key
        names = []
        in_watch = False
        with open(config_path, encoding="utf-8") as f:
            for line in f:
                stripped = line.rstrip()
                if stripped.strip() == "watch:":
                    in_watch = True
                elif in_watch and stripped.startswith("  - "):
                    names.append(stripped[4:].strip().strip('"\''))
                elif in_watch and stripped and not stripped.startswith(" "):
                    in_watch = False

    return {n.lower() for n in names}


# ── Daily file paths ──────────────────────────────────────────────────────────

def daily_paths(root: Path, d: date) -> dict:
    ds = d.strftime("%Y-%m-%d")
    return {
        "date":      d,
        "summary":   root / "detections" / f"summary_{ds}.csv",
        "incidents": root / "detections" / f"incidents_{ds}.csv",
        "wav_base":  root / "samples" / ds,
    }


# ── Summary CSV ───────────────────────────────────────────────────────────────

def load_summary(path: Path) -> dict:
    """Load an existing daily summary CSV into memory (supports script restarts)."""
    counts: dict[str, dict] = {}
    if not path.exists():
        return counts
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            counts[row["common_name"]] = {
                "common_name":     row["common_name"],
                "scientific_name": row["scientific_name"],
                "count":           int(row["count"]),
                "first_seen":      row["first_seen"],
                "last_seen":       row["last_seen"],
            }
    return counts


def save_summary(path: Path, counts: dict):
    """Rewrite the summary CSV, sorted by count descending."""
    rows = sorted(counts.values(), key=lambda r: r["count"], reverse=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)  # atomic on Windows too


def update_summary(counts: dict, detection: dict, ts: str):
    name = detection.get("common_name", "")
    if name not in counts:
        counts[name] = {
            "common_name":     name,
            "scientific_name": detection.get("scientific_name", ""),
            "count":           0,
            "first_seen":      ts,
            "last_seen":       ts,
        }
    counts[name]["count"]    += 1
    counts[name]["last_seen"] = ts


# ── Incident CSV ──────────────────────────────────────────────────────────────

def append_incident(path: Path, ts: str, detection: dict, wav_file: str):
    write_header = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=INCIDENT_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({
            "timestamp":       ts,
            "common_name":     detection.get("common_name", ""),
            "scientific_name": detection.get("scientific_name", ""),
            "confidence":      round(detection.get("confidence", 0.0), 4),
            "wav_file":        wav_file,
        })


# ── Audio helpers ─────────────────────────────────────────────────────────────

def list_devices():
    print("\nAvailable audio input devices:")
    print("-" * 60)
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            print(f"  [{i:2d}] {dev['name']}")
            print(f"       channels={dev['max_input_channels']}  "
                  f"default_samplerate={int(dev['default_samplerate'])}")
    print()


def find_device_index(name_fragment: str) -> int:
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and name_fragment.lower() in dev["name"].lower():
            return i
    raise ValueError(
        f"No input device matching '{name_fragment}' found. "
        "Run --list-devices to see available devices."
    )


def record_chunk(device_index: int, n_channels: int, channel_arg: str) -> np.ndarray:
    frames = int(SAMPLE_RATE * CHUNK_SECONDS)
    audio = sd.rec(
        frames,
        samplerate=SAMPLE_RATE,
        channels=n_channels,
        dtype="float32",
        device=device_index,
        blocking=True,
    )
    if channel_arg == "all":
        return audio.mean(axis=1)
    ch = int(channel_arg)
    if ch >= n_channels:
        raise ValueError(
            f"Device only has {n_channels} channel(s); --channel {ch} is out of range."
        )
    return audio[:, ch]


def analyse_chunk(
    analyzer: Analyzer,
    audio: np.ndarray,
    chunk_dt: datetime,
    use_geo: bool,
    threshold: float,
) -> list[dict]:
    """Write audio to a temp WAV, run BirdNET, return detections. Cleans up temp file."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name
    try:
        sf.write(tmp_path, audio, SAMPLE_RATE, subtype="PCM_16")
        kwargs: dict = {"min_conf": threshold}
        if use_geo:
            kwargs.update(lat=BRISBANE_LAT, lon=BRISBANE_LON, date=chunk_dt.date())
        recording = Recording(analyzer, tmp_path, **kwargs)
        with open(os.devnull, "w") as devnull:
            with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
                recording.analyze()
        return recording.detections or []
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def save_wav(audio: np.ndarray, chunk_dt: datetime, wav_base: Path, species_name: str) -> str:
    """Save audio to samples/YYYY-MM-DD/<species>/mic_YYYY-MM-DD_HHMMSS.wav.
    Returns the saved path as a string.
    """
    safe_name = species_name.replace("/", "-").replace("\\", "-")
    folder = wav_base / safe_name
    folder.mkdir(parents=True, exist_ok=True)
    fname = chunk_dt.strftime("mic_%Y-%m-%d_%H%M%S.wav")
    out_path = folder / fname
    sf.write(str(out_path), audio, SAMPLE_RATE, subtype="PCM_16")
    return str(out_path)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="BirdNET live detection with watch-list and daily file rotation."
    )
    parser.add_argument("--list-devices", action="store_true",
                        help="Print available audio input devices and exit.")
    parser.add_argument("--device", default="ReSpeaker",
                        help="Device name fragment to match (default: 'ReSpeaker').")
    parser.add_argument("--channel", default="0",
                        help="Mic channel (0-based int) or 'all' to average. Default: 0.")
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="Minimum BirdNET confidence to log (default: 0.5).")
    parser.add_argument("--geo", action="store_true",
                        help="Apply Brisbane geo/season filter to reduce false positives.")
    parser.add_argument("--root", default="birdwatch",
                        help="Root output folder (default: birdwatch). "
                             "CSVs go to <root>/detections/, WAVs to <root>/samples/.")
    parser.add_argument(
        "--species-config",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "species_config.yaml"),
        help="Path to species_config.yaml (default: same folder as this script).",
    )
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return

    root = Path(args.root)
    (root / "detections").mkdir(parents=True, exist_ok=True)
    (root / "samples").mkdir(parents=True, exist_ok=True)

    watch_list = load_watch_list(args.species_config)

    # Resolve device
    try:
        device_index = int(args.device)
    except ValueError:
        device_index = find_device_index(args.device)

    dev_info   = sd.query_devices(device_index)
    n_channels = dev_info["max_input_channels"]

    print(f"\nBirdNET watch-list detection")
    print(f"  Device     : [{device_index}] {dev_info['name']} ({n_channels} ch)")
    print(f"  Channel    : {args.channel}")
    print(f"  Threshold  : {args.threshold}")
    print(f"  Geo filter : {'Brisbane' if args.geo else 'OFF'}")
    print(f"  Detections : {(root / 'detections').resolve()}")
    print(f"  Samples    : {(root / 'samples').resolve()}")
    if watch_list:
        print(f"  Watching   : {', '.join(sorted(watch_list))}")
    else:
        print(f"  Watching   : (none — all species counted only)")
    print(f"\nLoading BirdNET model…", end=" ", flush=True)

    analyzer = Analyzer()
    print("ready.\n")

    # Initialise daily state
    paths  = daily_paths(root, date.today())
    counts = load_summary(paths["summary"])   # resume if restarted mid-day

    print(f"  {'Time':<10s}  {'Species':<35s}  {'Conf'}  {'Note'}")
    print("  " + "-" * 68)

    try:
        while True:
            chunk_dt = datetime.now()

            # Midnight rollover
            if chunk_dt.date() != paths["date"]:
                paths  = daily_paths(root, chunk_dt.date())
                counts = {}
                print(f"\n── {chunk_dt.strftime('%Y-%m-%d')} ──\n")

            audio      = record_chunk(device_index, n_channels, args.channel)
            detections = analyse_chunk(analyzer, audio, chunk_dt, args.geo, args.threshold)

            if not detections:
                continue

            ts = chunk_dt.strftime("%Y-%m-%d %H:%M:%S")

            # Save WAV once per chunk if any watched species present.
            # Filed under the first watched species detected.
            watched_hits = [
                d for d in detections
                if d.get("common_name", "").lower() in watch_list
            ]
            wav_path = ""
            if watched_hits:
                primary  = watched_hits[0].get("common_name", "Unknown")
                wav_path = save_wav(audio, chunk_dt, paths["wav_base"], primary)

            for d in detections:
                name       = d.get("common_name", "")
                is_watched = name.lower() in watch_list
                conf       = d.get("confidence", 0.0)
                note       = "*** WATCH ***" if is_watched else ""

                print(
                    f"  {chunk_dt.strftime('%H:%M:%S')}  "
                    f"{name:<35s}  {conf:.2f}  {note}"
                )

                update_summary(counts, d, ts)
                if is_watched:
                    append_incident(paths["incidents"], ts, d, wav_path)

            save_summary(paths["summary"], counts)

    except KeyboardInterrupt:
        print(f"\n\nStopped.")
        print(f"  Summary  : {paths['summary']}")
        if paths["incidents"].exists():
            print(f"  Incidents: {paths['incidents']}")


if __name__ == "__main__":
    main()
