"""
export_species_params.py — Dump species_tdoa_params to config/species_tdoa_params.json.

Usage:
    # Export the live DB's tuned species rows to the tracked config file
    python tools/export_species_params.py

    # Export to a different location instead (doesn't touch the tracked file)
    python tools/export_species_params.py --out /tmp/check.json

Run from the sound-hub root directory. Uses only stdlib — no venv needed.

config/species_tdoa_params.json is the version-controlled snapshot of this
table — the portable part of species_tdoa_params tuning (see
docs/tdoa-correlation-design-notes.md and tools/README.md). The DB is the
live working copy (edited via the Settings tab, or PUT
/species-tdoa-params/{key}); running this script is the "prepare a commit"
step — review the diff with `git diff config/species_tdoa_params.json`
before committing.

The '__default__' sentinel row is intentionally excluded — it's already
owned by FACTORY_DEFAULT_SPECIES_PARAMS in server/db.py, and re-exporting it
here would just create a second, driftable source of truth for the same
value.
"""

import argparse
import json
import os
import sqlite3


DB_PATH = os.path.join(os.path.dirname(__file__), "..", "sound_hub.db")
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "species_tdoa_params.json")
DEFAULT_SPECIES_KEY = "__default__"

# Column order matches species_tdoa_params in server/db.py's CREATE TABLE
# (minus species_key, which is the dict key rather than a field here).
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


def export_params(db_path: str) -> dict:
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "SELECT * FROM species_tdoa_params WHERE species_key != ? ORDER BY species_key",
            (DEFAULT_SPECIES_KEY,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    species = {}
    for row in rows:
        d = dict(row)
        species_key = d.pop("species_key")
        d["enabled"] = bool(d["enabled"])
        species[species_key] = {col: d[col] for col in COLUMNS}

    return {
        "schema_version": 1,
        "notes": (
            "freq_band_low_hz/freq_band_high_hz are derived from reference-"
            "recording acoustics and should transfer across deployments. "
            "onset_threshold_factor reflects the source recordings' own "
            "background noise, not any particular property's field SNR -- "
            "treat it as a starting point and re-validate against real "
            "pulled TDOA attempts before trusting it long-term. See "
            "tools/README.md."
        ),
        "species": species,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Export species_tdoa_params (minus the __default__ sentinel) to a JSON file."
    )
    parser.add_argument("--db", default=DB_PATH, help=f"Path to sound_hub.db (default: {DB_PATH})")
    parser.add_argument("--out", default=OUT_PATH, help=f"Output JSON path (default: {OUT_PATH})")
    args = parser.parse_args()

    data = export_params(args.db)
    out_path = os.path.abspath(args.out)
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")

    print(f"Exported {len(data['species'])} species to {out_path}")
    if out_path == os.path.abspath(OUT_PATH):
        print("This is the tracked config file -- review with `git diff` before committing.")


if __name__ == "__main__":
    main()
