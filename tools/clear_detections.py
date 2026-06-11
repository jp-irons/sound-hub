"""
clear_detections.py — Remove detection records from sound_hub.db.

Usage:
    # Preview what would be deleted (dry run)
    python tools/clear_detections.py --dry-run

    # Clear all detections
    python tools/clear_detections.py --all

    # Clear detections older than N days
    python tools/clear_detections.py --older-than 7

    # Clear detections from a specific source (filename substring)
    python tools/clear_detections.py --source soundscape

Run from the sound-hub root directory. Uses only stdlib — no venv needed.
"""

import argparse
import os
import sqlite3
from datetime import datetime, timedelta, timezone


DB_PATH = os.path.join(os.path.dirname(__file__), "..", "sound_hub.db")


def _connect() -> sqlite3.Connection:
    path = os.path.abspath(DB_PATH)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Database not found: {path}")
    return sqlite3.connect(path)


def _preview(conn: sqlite3.Connection, where: str, params: tuple):
    cur = conn.execute(f"SELECT COUNT(*) FROM detections WHERE {where}", params)
    (n,) = cur.fetchone()
    cur = conn.execute(
        f"SELECT analyzed_at, common_name, confidence, source FROM detections WHERE {where} ORDER BY analyzed_at DESC LIMIT 10",
        params,
    )
    rows = cur.fetchall()
    print(f"\n  {n} record(s) would be deleted.")
    if rows:
        print(f"\n  Most recent matches:")
        for at, name, conf, src in rows:
            print(f"    {at}  {name:<35s}  {conf:.2f}  {src or '—'}")
        if n > 10:
            print(f"    … and {n - 10} more")
    return n


def main():
    parser = argparse.ArgumentParser(description="Clear BirdNET detections from sound_hub.db.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true",
                       help="Delete all detections.")
    group.add_argument("--older-than", type=int, metavar="DAYS",
                       help="Delete detections older than N days.")
    group.add_argument("--source", metavar="SUBSTRING",
                       help="Delete detections whose source filename contains SUBSTRING.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be deleted without making changes.")
    args = parser.parse_args()

    conn = _connect()

    if args.all:
        where, params = "1=1", ()
    elif args.older_than:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.older_than)).isoformat()
        where, params = "analyzed_at < ?", (cutoff,)
    else:
        where, params = "source LIKE ?", (f"%{args.source}%",)

    n = _preview(conn, where, params)

    if n == 0:
        print("\n  Nothing to delete.")
        conn.close()
        return

    if args.dry_run:
        print("\n  Dry run — no changes made. Remove --dry-run to delete.")
        conn.close()
        return

    confirm = input(f"\n  Delete {n} record(s)? [y/N] ").strip().lower()
    if confirm != "y":
        print("  Aborted.")
        conn.close()
        return

    conn.execute(f"DELETE FROM detections WHERE {where}", params)
    conn.commit()
    print(f"  Deleted {n} record(s).")
    conn.close()


if __name__ == "__main__":
    main()
