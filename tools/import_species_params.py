"""
import_species_params.py — Load config/species_tdoa_params.json into
species_tdoa_params.

Usage:
    # Preview what would be added to the local DB
    python tools/import_species_params.py --dry-run

    # Import, skipping any species that already has a row
    python tools/import_species_params.py

    # Re-import everything, replacing existing rows with the tracked file's values
    python tools/import_species_params.py --overwrite

Run from the sound-hub root directory. Uses only stdlib — no venv needed.

Companion to export_species_params.py. Existing species rows are left alone
by default — this is meant for seeding a fresh instance (or adding species a
given instance hasn't configured yet) without silently clobbering local
hand-tuning that's drifted from the tracked file. Pass --overwrite to force
a species back to the tracked file's values.

The '__default__' sentinel is never touched here — see
export_species_params.py's docstring for why it's out of scope for this
file.
"""

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone


DB_PATH = os.path.join(os.path.dirname(__file__), "..", "sound_hub.db")
IN_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "species_tdoa_params.json")
DEFAULT_SPECIES_KEY = "__default__"

# Column order matches species_tdoa_params in server/db.py's CREATE TABLE
# (minus species_key, which is the dict key rather than a field here).
# updated_at is included so the INSERT statement lines up column-for-column
# with COLUMNS, but its value is always overwritten with "now" at write time
# (see main()) rather than taken from the file — it should reflect when this
# row was actually written into *this* DB, not when the tracked file was
# generated.
COLUMNS = [
    "enabled", "correlation_method", "onset_detection_method",
    "onset_threshold_factor", "freq_band_low_hz", "freq_band_high_hz",
    "window_margin_pre_ms", "window_margin_post_ms",
    "min_corroborating_nodes", "notes", "updated_at",
]


def _connect(path: str) -> sqlite3.Connection:
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Database not found: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _existing_species_keys(conn: sqlite3.Connection) -> set:
    cur = conn.execute("SELECT species_key FROM species_tdoa_params")
    return {row[0] for row in cur.fetchall()}


def main():
    parser = argparse.ArgumentParser(
        description="Import species_tdoa_params rows from a JSON file (see export_species_params.py)."
    )
    parser.add_argument("--db", default=DB_PATH, help=f"Path to sound_hub.db (default: {DB_PATH})")
    parser.add_argument("--in", dest="in_path", default=IN_PATH,
                        help=f"Input JSON path (default: {IN_PATH})")
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace existing species rows instead of skipping them.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing to the DB.")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Skip the confirmation prompt.")
    args = parser.parse_args()

    in_path = os.path.abspath(args.in_path)
    if not os.path.exists(in_path):
        raise FileNotFoundError(f"Input file not found: {in_path}")
    with open(in_path) as f:
        data = json.load(f)

    species = dict(data.get("species", {}))
    species.pop(DEFAULT_SPECIES_KEY, None)  # never imported, see module docstring

    conn = _connect(args.db)
    try:
        existing = _existing_species_keys(conn)

        to_write = {}
        skipped = []
        for species_key, params in species.items():
            if species_key in existing and not args.overwrite:
                skipped.append(species_key)
                continue
            to_write[species_key] = params

        print(f"{len(to_write)} species will be written, {len(skipped)} skipped (already present):")
        for k in sorted(to_write):
            action = "overwrite" if k in existing else "insert"
            print(f"  [{action:9s}] {k}")
        for k in sorted(skipped):
            print(f"  [skip     ] {k}  (already exists -- use --overwrite to replace)")

        if not to_write:
            print("\nNothing to do.")
            return

        if args.dry_run:
            print("\nDry run -- no changes made. Remove --dry-run to write.")
            return

        if not args.yes:
            confirm = input(
                f"\nWrite {len(to_write)} row(s) to {os.path.abspath(args.db)}? [y/N] "
            ).strip().lower()
            if confirm != "y":
                print("Aborted.")
                return

        now = datetime.now(timezone.utc).isoformat()
        for species_key, params in to_write.items():
            values = [params.get(col) for col in COLUMNS[:-1]] + [now]
            conn.execute(
                f"""INSERT OR REPLACE INTO species_tdoa_params
                    (species_key, {", ".join(COLUMNS)})
                    VALUES (?, {", ".join("?" for _ in COLUMNS)})""",
                [species_key] + values,
            )
        conn.commit()
        print(f"\nWrote {len(to_write)} row(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
