#!/usr/bin/env python3
"""Aggregate report over TDOA attempts since the 2026-07-23 fixes
(AMBIGUOUS_RATIO_THRESHOLD 1.5->1.2 @ ~10:20 Brisbane, least_squares
conditioning check @ ~10:41 Brisbane). Answers: how many attempts have run,
where are they dying (node dropout vs correlation vs solver), is the
correlation trust rate what we expect, and -- most importantly -- do any
solved attempts hold up physically.

Run on the hub VM:
    python3 tools/tdoa_since_fix_report.py [cutoff_brisbane_HH:MM]
    (default cutoff: 10:20 today, Brisbane time, UTC+10, no DST)
"""
import sqlite3
import sys
import math
import re
from datetime import datetime, timezone, timedelta

DB_PATH = "/opt/sound-hub/sound_hub.db"
C = 343.0  # m/s, matches tdoa_solver.DEFAULT_SPEED_OF_SOUND
BRISBANE_OFFSET = timedelta(hours=10)

cutoff_hhmm = sys.argv[1] if len(sys.argv) > 1 else "10:20"
now_brisbane = datetime.now(timezone.utc) + BRISBANE_OFFSET
hh, mm = (int(x) for x in cutoff_hhmm.split(":"))
cutoff_brisbane = now_brisbane.replace(hour=hh, minute=mm, second=0, microsecond=0)
cutoff_utc = cutoff_brisbane - BRISBANE_OFFSET
cutoff_us = int(cutoff_utc.timestamp() * 1_000_000)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

print(f"=== TDOA attempts since {cutoff_brisbane.strftime('%Y-%m-%d %H:%M')} Brisbane "
      f"({(now_brisbane - cutoff_brisbane)}) ===\n")

attempts = conn.execute(
    "SELECT * FROM tdoa_attempts WHERE t_start_us >= ? ORDER BY id",
    (cutoff_us,),
).fetchall()
print(f"Total attempts: {len(attempts)}\n")

# --- Funnel by status ---
by_status = {}
for a in attempts:
    by_status.setdefault(a["status"], []).append(a)
print("--- By status ---")
for status, rows in sorted(by_status.items(), key=lambda kv: -len(kv[1])):
    print(f"  {status:12s} {len(rows)}")

# --- Failure reason buckets ---
def bucket_reason(reason):
    if not reason:
        return "unknown"
    if re.search(r"node\(s\)", reason):
        return "node_count_dropout"
    if "discriminant" in reason:
        return "negative_discriminant"
    if "ill-conditioned" in reason:
        return "ill_conditioned (NEW conditioning check)"
    if "singular" in reason or "rank" in reason:
        return "singular_geometry"
    return "other: " + reason[:60]

failed = by_status.get("failed", [])
if failed:
    print(f"\n--- Failure reasons ({len(failed)} failed attempts) ---")
    buckets = {}
    for a in failed:
        b = bucket_reason(a["failure_reason"])
        buckets.setdefault(b, 0)
        buckets[b] += 1
    for b, n in sorted(buckets.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  {b}")

# --- Correlation trust rate (arrived rows belonging to these attempts) ---
attempt_ids = [a["id"] for a in attempts]
if attempt_ids:
    placeholders = ",".join("?" * len(attempt_ids))
    rows = conn.execute(
        f"""
        SELECT peak_corr_coef, quality_ratio
        FROM tdoa_attempt_nodes
        WHERE attempt_id IN ({placeholders}) AND status = 'arrived'
        """,
        attempt_ids,
    ).fetchall()
    no_corr = sum(1 for r in rows if r["peak_corr_coef"] is None)
    failed_coef = sum(1 for r in rows if r["peak_corr_coef"] is not None and r["peak_corr_coef"] < 0.3)
    failed_ambig = sum(
        1 for r in rows
        if r["peak_corr_coef"] is not None and r["peak_corr_coef"] >= 0.3
        and r["quality_ratio"] is not None and r["quality_ratio"] < 1.2
    )
    trusted = sum(
        1 for r in rows
        if r["peak_corr_coef"] is not None and r["peak_corr_coef"] >= 0.3
        and r["quality_ratio"] is not None and r["quality_ratio"] >= 1.2
    )
    print(f"\n--- Correlation outcomes on 'arrived' rows ({len(rows)} total) ---")
    print(f"  no_correlation_attempted: {no_corr}")
    print(f"  failed_coef (<0.3):       {failed_coef}")
    print(f"  failed_ambiguity (<1.2):  {failed_ambig}")
    print(f"  trusted (>=1.2):          {trusted}")

# --- Solved attempts: full detail + physical plausibility check ---
positions = {
    row["node_id"]: (row["pos_e"], row["pos_n"], row["pos_alt"])
    for row in conn.execute(
        "SELECT node_id, pos_e, pos_n, pos_alt FROM node_positions "
        "WHERE pos_e IS NOT NULL AND pos_n IS NOT NULL AND pos_alt IS NOT NULL"
    )
}

solved = by_status.get("solved", [])
print(f"\n--- Solved attempts ({len(solved)}) ---")
if not solved:
    print("  (none yet)")
for a in solved:
    print(f"\n  attempt {a['id']} species={a['species_key']} solved_at={a['solved_at']}")
    print(f"    position: E={a['solved_e']:.2f} N={a['solved_n']:.2f} Alt={a['solved_alt']:.2f} "
          f"(+/-{a['solve_residual_m']:.2f}m, {a['solve_method']})")

    nodes = conn.execute(
        "SELECT node_id, arrival_us FROM tdoa_attempt_nodes "
        "WHERE attempt_id = ? AND status = 'arrived' AND arrival_us IS NOT NULL",
        (a["id"],),
    ).fetchall()
    usable = [n for n in nodes if n["node_id"] in positions]
    worst_ratio = 0.0
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            ni, nj = usable[i], usable[j]
            xi, yi, zi = positions[ni["node_id"]]
            xj, yj, zj = positions[nj["node_id"]]
            baseline_m = math.sqrt((xi - xj) ** 2 + (yi - yj) ** 2 + (zi - zj) ** 2)
            dt_us = ni["arrival_us"] - nj["arrival_us"]
            implied_m = C * dt_us * 1e-6
            ratio = abs(implied_m) / baseline_m if baseline_m > 0 else float("inf")
            worst_ratio = max(worst_ratio, ratio)
    plausible = (
        a["solve_residual_m"] is not None and a["solve_residual_m"] < 50
        and worst_ratio <= 1.0
    )
    print(f"    worst pairwise ratio: {worst_ratio:.2f}  "
          f"{'PLAUSIBLE' if plausible else 'STILL LOOKS WRONG'}")

conn.close()
