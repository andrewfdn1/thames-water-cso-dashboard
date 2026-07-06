#!/usr/bin/env python3
"""
Diagnostic tool for investigating suspicious patterns (flatlines, stuck-open
records, mismatched Start/Stop pairs) in discharge_history.db -- gathers
evidence before proposing a fix, rather than guessing.

Two modes:

  Per-watercourse deep dive (lists every raw event for every permit
  matching a watercourse name, with flags):
      python3 scripts/diagnose_watercourse.py "Ampney Brook"

  Database-wide audit (scans every permit for the same red flags, to see
  whether an issue is isolated or widespread):
      python3 scripts/diagnose_watercourse.py --all

What it flags, and why each one matters:
  - STILL OPEN: no Stop event recorded. Expected in small numbers for very
    recent starts; a red flag if the start is old (see the Summary page's
    own unclosed-discharge warning, which uses the same reasoning).
  - LONG (>24h): a single interval lasting more than a day. Not impossible
    (a real prolonged storm event), but atypical enough to be worth a
    second look -- most discharges in this dataset run minutes to a few
    hours.
  - OVERLAPS previous: this permit's Start happened before its previous
    event's Stop. Since at most one open discharge per permit is assumed
    throughout this codebase (matching how the live app treats a permit's
    status as a single current state), an overlap means two events got
    process_chunk-paired incorrectly -- a real pairing bug, not a data
    gap.
  - Repeating exact duration: if several of a permit's intervals share the
    *exact* same duration (to the second), that's a strong signal of a
    systematic generation/pairing bug rather than genuine variability --
    real discharge durations vary continuously with rainfall, they don't
    repeat identically.
  - CROSS-PERMIT repeating exact duration (deep dive only): the same check
    as above, but across every permit matching the watercourse rather than
    within a single permit's own history. A watercourse is usually fed by
    several permits/monitors, so a pairing bug that strikes more than one
    of them at once won't show up as a within-permit repeat (each affected
    permit might only have one bad interval) but will show up here.
"""
import sys
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

os.environ.setdefault("DISCHARGE_AUTO_SYNC", "0")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
import app as appmod  # noqa: E402


def _analyse_events(rows):
    """rows: list of (start_utc, stop_utc, retry_count) for ONE permit,
    already sorted by start_utc ascending. Returns (lines, flags_summary)."""
    lines = []
    durations = []
    long_count = 0
    open_count = 0
    overlap_count = 0
    prev_stop = None

    for start_utc, stop_utc, retry_count in rows:
        start_dt = datetime.fromisoformat(start_utc)
        stop_dt = datetime.fromisoformat(stop_utc) if stop_utc else None
        end_for_duration = stop_dt or datetime.now(timezone.utc)
        dur_hours = (end_for_duration - start_dt).total_seconds() / 3600

        flags = []
        if stop_dt is None:
            flags.append("STILL OPEN")
            open_count += 1
        if dur_hours > 24:
            flags.append(f"LONG ({dur_hours:.1f}h)")
            long_count += 1
        if prev_stop is not None and start_dt < prev_stop:
            flags.append(f"OVERLAPS previous (prev stop {prev_stop.isoformat()})")
            overlap_count += 1

        durations.append(round(dur_hours, 2))
        lines.append(
            f"  {start_utc}  ->  {(stop_utc or 'NULL'):30}  {dur_hours:9.2f}h  "
            f"retries={retry_count}  {' '.join(flags)}"
        )
        prev_stop = stop_dt if stop_dt is not None else prev_stop

    dur_counts = Counter(durations)
    repeats = {d: c for d, c in dur_counts.items() if c >= 3}

    summary = {
        "total": len(rows),
        "open": open_count,
        "long": long_count,
        "overlaps": overlap_count,
        "repeats": repeats,
    }
    return lines, summary


def deep_dive(watercourse_query):
    monitors, _ = appmod.get_all_monitors()
    monitors = monitors or {}
    target = watercourse_query.strip().lower()
    matching = {p: m for p, m in monitors.items() if target in (m.get("water") or "").lower()}

    print(f"Permits matching '{watercourse_query}': {len(matching)}")
    for p, m in matching.items():
        print(f"  {p}  {m.get('name')!r}  water={m.get('water')!r}  tunnel={m.get('tunnel_connected_inferred')}")
    print()

    if not matching:
        print("No matching permits in the CURRENT monitor list, so nothing to cross-reference.")
        print("(A permit that's been renamed/retired since would need its old name/permit number instead.)")
        return

    conn = appmod._discharge_backfill.db_connect()
    try:
        cross_permit_durations = defaultdict(list)  # duration_hours -> [(permit, start_utc, stop_utc), ...]
        for permit in matching:
            rows = conn.execute(
                "SELECT start_utc, stop_utc, retry_count FROM discharge_events WHERE permit = ? ORDER BY start_utc ASC",
                (permit,),
            ).fetchall()
            print(f"=== {permit} ({len(rows)} events) ===")
            lines, summary = _analyse_events(rows)
            for line in lines:
                print(line)
            print(
                f"  -> {summary['open']} still open, {summary['long']} over 24h, "
                f"{summary['overlaps']} overlapping previous event"
            )
            if summary["repeats"]:
                print(f"  -> SUSPICIOUS repeating exact durations (hours: count): {summary['repeats']}")
            print()

            for start_utc, stop_utc, _retry_count in rows:
                start_dt = datetime.fromisoformat(start_utc)
                stop_dt = datetime.fromisoformat(stop_utc) if stop_utc else None
                end_for_duration = stop_dt or datetime.now(timezone.utc)
                dur_hours = round((end_for_duration - start_dt).total_seconds() / 3600, 2)
                cross_permit_durations[dur_hours].append((permit, start_utc, stop_utc))
    finally:
        conn.close()

    cross_repeats = {d: evs for d, evs in cross_permit_durations.items() if len(evs) >= 3}
    if cross_repeats:
        print("=== CROSS-PERMIT check: same exact duration recurring across different permits ===")
        print(
            "(A systematic pairing/generation bug is more likely to produce the same duration on "
            "several different permits than one permit is to repeat it against itself.)"
        )
        for dur_hours, evs in sorted(cross_repeats.items(), key=lambda x: -len(x[1])):
            permits_involved = sorted(set(p for p, _, _ in evs))
            print(f"  {dur_hours}h occurs {len(evs)} times across {len(permits_involved)} permit(s): {permits_involved}")
            for permit, start_utc, stop_utc in evs:
                print(f"    {permit}  {start_utc}  ->  {stop_utc or 'NULL'}")
        print()


def full_audit():
    conn = appmod._discharge_backfill.db_connect()
    try:
        rows = conn.execute(
            "SELECT permit, start_utc, stop_utc, retry_count FROM discharge_events ORDER BY permit, start_utc ASC"
        ).fetchall()
    finally:
        conn.close()

    by_permit = defaultdict(list)
    for permit, start_utc, stop_utc, retry_count in rows:
        by_permit[permit].append((start_utc, stop_utc, retry_count))

    print(f"Auditing {len(by_permit)} permits, {len(rows)} total events...\n")

    flagged_overlap = []
    flagged_long = []
    flagged_repeat = []

    for permit, permit_rows in by_permit.items():
        _, summary = _analyse_events(permit_rows)
        if summary["overlaps"]:
            flagged_overlap.append((permit, summary["overlaps"], summary["total"]))
        if summary["long"] >= 3:   # a handful of long events can be real storms; a pattern is more telling
            flagged_long.append((permit, summary["long"], summary["total"]))
        if summary["repeats"]:
            flagged_repeat.append((permit, summary["repeats"]))

    print(f"## Permits with overlapping (mispaired) intervals: {len(flagged_overlap)}")
    for permit, n, total in sorted(flagged_overlap, key=lambda x: -x[1])[:30]:
        print(f"  {permit}: {n} overlaps out of {total} events")

    print(f"\n## Permits with 3+ events over 24h: {len(flagged_long)}")
    for permit, n, total in sorted(flagged_long, key=lambda x: -x[1])[:30]:
        print(f"  {permit}: {n} long events out of {total}")

    print(f"\n## Permits with a repeating exact duration (3+ times): {len(flagged_repeat)}")
    for permit, repeats in sorted(flagged_repeat, key=lambda x: -max(x[1].values()))[:30]:
        print(f"  {permit}: {repeats}")

    print(
        "\nRun the per-watercourse deep dive on any of these permits' watercourse "
        "for the full event-by-event detail:\n"
        '  python3 scripts/diagnose_watercourse.py "<watercourse name>"'
    )


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--all":
        full_audit()
    elif len(sys.argv) == 2:
        deep_dive(sys.argv[1])
    else:
        print(__doc__)
        sys.exit(1)
