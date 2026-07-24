#!/usr/bin/env python3
"""Aggregate report over recent TDOA attempts. Answers: how many attempts
have run, where are they dying (node dropout vs correlation vs solver), is
the correlation trust rate what we expect, and -- most importantly -- do
any solved attempts hold up physically.

Run on the hub VM:
    python3 tools/tdoa_since_fix_report.py [cutoff]
    default cutoff: last 24 hours (rolling window from now)

    cutoff can be:
      - omitted                  -> now minus 24h
      - "HH:MM"                  -> that time today, Brisbane (UTC+10, no DST)
      - "YYYY-MM-DD HH:MM"       -> that exact Brisbane date/time, e.g. the
                                     2026-07-23 10:20 fix-deployment reference
                                     point from that day's investigation
"""
import sqlite3
import sys
import math
import re
from datetime import datetime, timezone, timedelta

DB_PATH = "/opt/sound-hub/sound_hub.db"
C = 343.0  # m/s, matches tdoa_solver.DEFAULT_SPEED_OF_SOUND
BRISBANE_OFFSET = timedelta(hours=10)

cutoff_arg = sys.argv[1] if len(sys.argv) > 1 else None
now_brisbane = datetime.now(timezone.utc) + BRISBANE_OFFSET

if cutoff_arg is None:
    # Rolling window, not a fixed time-of-day -- a fixed "HH:MM" default
    # silently reuses *today's* date every run, so it drifts to "today" and
    # stops meaning anything the day after it was written (confirmed bug,
    # 2026-07-24: defaulted to a cutoff a few minutes in the *future*).
    cutoff_brisbane = now_brisbane - timedelta(hours=24)
elif re.fullmatch(r"\d{1,2}:\d{2}", cutoff_arg):
    hh, mm = (int(x) for x in cutoff_arg.split(":"))
    cutoff_brisbane = now_brisbane.replace(hour=hh, minute=mm, second=0, microsecond=0)
else:
    # "YYYY-MM-DD HH:MM" (also accepts a "T" separator). tzinfo=utc here
    # doesn't mean this value IS utc -- it matches the same "aware but the
    # wall-clock number is actually Brisbane local time" convention used by
    # now_brisbane above (datetime.now(utc) + BRISBANE_OFFSET), required so
    # cutoff_utc = cutoff_brisbane - BRISBANE_OFFSET below computes a real
    # epoch timestamp via .timestamp() instead of silently reinterpreting a
    # naive value against the host's local system timezone.
    cutoff_brisbane = datetime.strptime(
        cutoff_arg.replace("T", " "), "%Y-%m-%d %H:%M"
    ).replace(tzinfo=timezone.utc)

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

# Array aperture: centroid + max pairwise baseline across ALL surveyed nodes,
# not just one attempt's subset -- gives a sense of how big the array
# actually is, which bounds how far away a source can be range-resolved at
# all (short baseline relative to source distance = range is barely
# observable, see note below).
if positions:
    cx = sum(p[0] for p in positions.values()) / len(positions)
    cy = sum(p[1] for p in positions.values()) / len(positions)
    cz = sum(p[2] for p in positions.values()) / len(positions)
    max_baseline = 0.0
    ids = list(positions)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            xi, yi, zi = positions[ids[i]]
            xj, yj, zj = positions[ids[j]]
            max_baseline = max(max_baseline, math.sqrt((xi-xj)**2 + (yi-yj)**2 + (zi-zj)**2))
    print(f"\n--- Array aperture ({len(positions)} surveyed nodes) ---")
    print(f"  centroid: E={cx:.2f} N={cy:.2f} Alt={cz:.2f}")
    print(f"  max pairwise baseline: {max_baseline:.2f}m")
    print("  (a solved position should be within roughly the property's own")
    print("   scale of this centroid -- baseline this short relative to a")
    print("   source tens/hundreds of metres away gives very weak range")
    print("   resolution: tiny, physically-valid timing noise can swing the")
    print("   solved distance from metres to thousands of km along the same")
    print("   bearing, even with a perfectly well-conditioned solve.)")
else:
    cx = cy = cz = 0.0

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
    origin_id = a["origin_node_id"]
    worst_ratio = 0.0
    worst_origin_ratio = 0.0
    worst_corrob_ratio = 0.0
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
            involves_origin = origin_id in (ni["node_id"], nj["node_id"])
            if involves_origin:
                worst_origin_ratio = max(worst_origin_ratio, ratio)
            else:
                worst_corrob_ratio = max(worst_corrob_ratio, ratio)
    dist_from_centroid = math.sqrt(
        (a["solved_e"] - cx) ** 2 + (a["solved_n"] - cy) ** 2 + (a["solved_alt"] - cz) ** 2
    )
    # 500m is generous for a 3ha property -- anything past that is not "far
    # corner of the array", it's wrong, regardless of residual/ratio.
    plausible = (
        a["solve_residual_m"] is not None and a["solve_residual_m"] < 50
        and worst_ratio <= 1.0
        and dist_from_centroid < 500
    )
    print(f"    distance from array centroid: {dist_from_centroid:.1f}m")
    print(f"    worst pairwise ratio: {worst_ratio:.2f}  "
          f"{'PLAUSIBLE' if plausible else 'STILL LOOKS WRONG'}")
    print(f"    worst origin-vs-corroborator ratio: {worst_origin_ratio:.2f}   "
          f"worst corroborator-vs-corroborator ratio: {worst_corrob_ratio:.2f}")

conn.close()
