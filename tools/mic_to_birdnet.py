"""
mic_to_birdnet.py — Live microphone → BirdNET detection → CSV

Captures audio from a USB mic (e.g. ReSpeaker 4-mic array), runs each chunk
through BirdNET-Analyzer, and appends detections to a CSV file.

Dependencies (install into sound-hub venv):
    pip install birdnet-analyzer birdnetlib sounddevice soundfile

Usage:
    # List available audio devices
    python tools/mic_to_birdnet.py --list-devices

    # Run with defaults (channel 0, threshold 0.5, no geo filter)
    python tools/mic_to_birdnet.py --device "ReSpeaker"

    # Enable Brisbane geo filter, lower threshold, specific output file
    python tools/mic_to_birdnet.py --device "ReSpeaker" --geo --threshold 0.3 --output my_session.csv

    # Average all 4 channels instead of using just channel 0
    python tools/mic_to_birdnet.py --device "ReSpeaker" --channel all
"""

import argparse
import contextlib
import csv
import os
import tempfile
from datetime import datetime, date

import numpy as np
import sounddevice as sd
import soundfile as sf
from birdnetlib import Recording
from birdnetlib.analyzer import Analyzer


# ── Constants ────────────────────────────────────────────────────────────────

SAMPLE_RATE = 48_000          # Hz — BirdNET native rate
CHUNK_SECONDS = 3             # BirdNET analysis window
BRISBANE_LAT = -27.5
BRISBANE_LON = 153.0

CSV_FIELDS = [
    "timestamp",              # ISO8601 wall-clock time of chunk start
    "chunk_start_sec",        # offset within chunk (always 0.0 for 3 s chunks)
    "chunk_end_sec",
    "common_name",
    "scientific_name",
    "confidence",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

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
    """Return the first input device whose name contains name_fragment (case-insensitive)."""
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and name_fragment.lower() in dev["name"].lower():
            return i
    raise ValueError(
        f"No input device matching '{name_fragment}' found. "
        "Run --list-devices to see available devices."
    )


def record_chunk(device_index: int, n_channels: int, channel_arg: str) -> np.ndarray:
    """Record one chunk and return a 1-D 48 kHz float32 numpy array."""
    frames = int(SAMPLE_RATE * CHUNK_SECONDS)
    audio = sd.rec(
        frames,
        samplerate=SAMPLE_RATE,
        channels=n_channels,
        dtype="float32",
        device=device_index,
        blocking=True,
    )
    # Mix down to mono
    if channel_arg == "all":
        mono = audio.mean(axis=1)
    else:
        ch = int(channel_arg)
        if ch >= n_channels:
            raise ValueError(f"Device only has {n_channels} channel(s); --channel {ch} is out of range.")
        mono = audio[:, ch]
    return mono


def analyse_chunk(
    analyzer: Analyzer,
    audio: np.ndarray,
    chunk_dt: datetime,
    use_geo: bool,
    threshold: float,
) -> list[dict]:
    """Write audio to a temp WAV, run BirdNET, return list of detection dicts."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name

    try:
        sf.write(tmp_path, audio, SAMPLE_RATE, subtype="PCM_16")

        kwargs = dict(min_conf=threshold)
        if use_geo:
            kwargs["lat"] = BRISBANE_LAT
            kwargs["lon"] = BRISBANE_LON
            kwargs["date"] = date(chunk_dt.year, chunk_dt.month, chunk_dt.day)

        recording = Recording(analyzer, tmp_path, **kwargs)
        with open(os.devnull, "w") as devnull:
            with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
                recording.analyze()
        return recording.detections or []
    finally:
        os.unlink(tmp_path)


def append_to_csv(path: str, chunk_dt: datetime, detections: list[dict]):
    write_header = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        for d in detections:
            writer.writerow({
                "timestamp":       chunk_dt.isoformat(timespec="seconds"),
                "chunk_start_sec": round(d.get("start_time", 0.0), 2),
                "chunk_end_sec":   round(d.get("end_time", CHUNK_SECONDS), 2),
                "common_name":     d.get("common_name", ""),
                "scientific_name": d.get("scientific_name", ""),
                "confidence":      round(d.get("confidence", 0.0), 4),
            })


def print_detections(chunk_dt: datetime, detections: list[dict]):
    for d in detections:
        bar = "█" * int(d.get("confidence", 0) * 20)
        print(
            f"  {chunk_dt.strftime('%H:%M:%S')}  "
            f"{d.get('common_name', '?'):<35s}  "
            f"{d.get('confidence', 0):.2f}  {bar}"
        )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Stream mic audio through BirdNET and log detections to CSV."
    )
    parser.add_argument("--list-devices", action="store_true",
                        help="Print available audio input devices and exit.")
    parser.add_argument("--device", default="ReSpeaker",
                        help="Device name fragment to match (default: 'ReSpeaker'). "
                             "Use the numeric index from --list-devices if name matching fails.")
    parser.add_argument("--channel", default="0",
                        help="Which mic channel to use (0-based int), or 'all' to average all channels. "
                             "Default: 0.")
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="Minimum confidence to log a detection (default: 0.5).")
    parser.add_argument("--geo", action="store_true",
                        help="Apply Brisbane geo/season filter to reduce false positives.")
    parser.add_argument("--output", default="detections.csv",
                        help="CSV output file path (default: detections.csv).")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return

    # Resolve device
    try:
        device_index = int(args.device)
    except ValueError:
        device_index = find_device_index(args.device)

    dev_info = sd.query_devices(device_index)
    n_channels = dev_info["max_input_channels"]

    print(f"\nBirdNET live detection")
    print(f"  Device   : [{device_index}] {dev_info['name']} ({n_channels} ch)")
    print(f"  Channel  : {args.channel}")
    print(f"  Threshold: {args.threshold}")
    print(f"  Geo filter: {'Brisbane (lat={BRISBANE_LAT}, lon={BRISBANE_LON})' if args.geo else 'OFF'}")
    print(f"  Output   : {args.output}")
    print(f"\nLoading BirdNET model…", end=" ", flush=True)

    analyzer = Analyzer()
    print("ready.\n")
    print(f"  {'Time':<10s}  {'Species':<35s}  {'Conf'}  {'Bar'}")
    print("  " + "-" * 70)

    try:
        while True:
            chunk_dt = datetime.now()
            audio = record_chunk(device_index, n_channels, args.channel)
            detections = analyse_chunk(analyzer, audio, chunk_dt, args.geo, args.threshold)
            print_detections(chunk_dt, detections)
            if detections:
                append_to_csv(args.output, chunk_dt, detections)
    except KeyboardInterrupt:
        print(f"\n\nStopped. Detections saved to: {args.output}")


if __name__ == "__main__":
    main()
