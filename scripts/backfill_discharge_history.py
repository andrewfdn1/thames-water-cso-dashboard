#!/usr/bin/env python3
"""
One-off, resumable backfill of national CSO discharge history into a local
SQLite database (data/discharge_history.db), so features like a "total
discharge over the last N months" chart don't depend on the live app's
30-day rolling window.

This is deliberately NOT part of the Flask app's request path -- it's a
standalone script you run manually from a terminal, expected to take a
while (the API can return a lot of data for some windows; a 180-day test
pull returned 12,600 items across 64 pages in ~90s), and safe to interrupt
(Ctrl+C) and re-run -- it checkpoints progress after every completed chunk
and picks up where it left off.

Usage:
    python3 scripts/backfill_discharge_history.py
    (run again any time to continue from the last checkpoint; it stops on
    its own once it reaches today)

Politeness / respectfulness toward a free public API:
  - Fixed-size chronological chunks (14 days), not one giant date range --
    keeps any single request/response reasonably sized even during a
    storm-heavy period, and gives natural, frequent checkpoints.
  - 1s pause between paginated pages (matches the main app's own pacing).
  - A pause between a chunk's Start-events and Stop-events fetch.
  - An adaptive pause between chunks, proportional to how much data the
    last chunk returned, so a heavy (storm) chunk gets more breathing room
    afterwards rather than being immediately followed by another request.
  - Retries on HTTP 429 with exponential backoff.
  - Documented rate limit is 5 requests/second per user; this script stays
    far under that on purpose.
"""
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, date, timedelta, timezone

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
import db  # noqa: E402 -- local sqlite3 by default, or Turso if configured (see db.py)

BASE = "https://api.thameswater.co.uk/opendata/v2/discharge"
HEADERS = {"User-Agent": "Mozilla/5.0"}
PAGE_LIMIT = 200
CHUNK_DAYS = 14

# The API documents historical data back to April 2022, but explicitly
# warns that visibility of events before the official "go live" date is
# limited/non-representative by design -- starting the backfill from
# go-live avoids treating that sparse pre-launch snapshot as real history.
GO_LIVE_DATE = date(2022, 12, 30)

DB_PATH = os.path.join(_PROJECT_ROOT, "data", "discharge_history.db")

try:
    import requests
except ImportError:
    print("This script needs the 'requests' package (already in requirements.txt).")
    print("Run it with the project's venv active: source venv/bin/activate")
    sys.exit(1)


def db_connect():
    conn = db.connect(DB_PATH, env_prefix="TURSO_DISCHARGE")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discharge_events (
            permit    TEXT NOT NULL,
            start_utc TEXT NOT NULL,
            stop_utc  TEXT,
            PRIMARY KEY (permit, start_utc)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backfill_progress (
            id               INTEGER PRIMARY KEY CHECK (id = 0),
            next_chunk_start TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        )
    """)
    return conn


def load_progress(conn):
    row = conn.execute("SELECT next_chunk_start FROM backfill_progress WHERE id = 0").fetchone()
    if row:
        return date.fromisoformat(row[0])
    return GO_LIVE_DATE


def save_progress(conn, next_chunk_start):
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO backfill_progress (id, next_chunk_start, updated_at)
            VALUES (0, ?, ?)
            ON CONFLICT(id) DO UPDATE SET next_chunk_start = excluded.next_chunk_start, updated_at = excluded.updated_at
            """,
            (next_chunk_start.isoformat(), now),
        )


def load_pending(conn):
    """Starts seen in an earlier chunk that had no matching Stop yet --
    reloaded here so resuming in a fresh process still pairs them
    correctly with a Stop that turns up in a later chunk."""
    pending = {}
    rows = conn.execute("SELECT permit, start_utc FROM discharge_events WHERE stop_utc IS NULL").fetchall()
    for permit, start_utc in rows:
        pending[permit] = datetime.fromisoformat(start_utc)
    return pending


def fetch_all_pages(url, params):
    items = []
    offset = 0
    pages = 0
    while True:
        page_params = dict(params, limit=PAGE_LIMIT, offset=offset)
        for attempt in range(5):
            r = requests.get(url, params=page_params, headers=HEADERS, timeout=30)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            break
        r.raise_for_status()
        page = r.json().get("items", [])
        items.extend(page)
        pages += 1
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        time.sleep(1)
    return items, pages


def fetch_chunk(params, max_attempts=3):
    """Fetch a chunk's Start+Stop events, retrying the whole chunk if both
    come back completely empty. Confirmed in practice (2026-06-12..2026-07-05
    initially came back as 0+0 during a long backfill run, then 259+24 and
    24+24 on immediate re-check) that this API can return a valid HTTP 200
    with zero items under sustained request load, which a single-shot fetch
    can't distinguish from a genuinely quiet period. Only retries when the
    result is suspiciously all-zero, so a normal chunk costs nothing extra."""
    starts, stops, total_pages = [], [], 0
    for attempt in range(1, max_attempts + 1):
        starts, start_pages = fetch_all_pages(BASE + "/alerts", dict(params, alertType="Start"))
        time.sleep(1)
        stops, stop_pages = fetch_all_pages(BASE + "/alerts", dict(params, alertType="Stop"))
        total_pages = start_pages + stop_pages
        if starts or stops or attempt == max_attempts:
            return starts, stops, total_pages
        print(
            f"    zero results for {params['dateStart']}..{params['dateEnd']} "
            f"(attempt {attempt}/{max_attempts}) -- retrying after a pause, in case "
            f"this is the API returning an empty response under load rather than a "
            f"genuinely quiet period"
        )
        time.sleep(10)
    return starts, stops, total_pages


def parse_dt(raw):
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def process_chunk(conn, starts, stops, pending):
    """Pair this chunk's Start/Stop events with each other and with any
    still-open start carried over from an earlier chunk. Assumes at most
    one open discharge per permit at a time, matching how the live app
    already treats a permit's status as a single current state."""
    events = defaultdict(list)
    for item in starts:
        permit, dt_str = item.get("permitNumber"), item.get("datetime")
        if permit and dt_str:
            events[permit].append((parse_dt(dt_str), "start"))
    for item in stops:
        permit, dt_str = item.get("permitNumber"), item.get("datetime")
        if permit and dt_str:
            events[permit].append((parse_dt(dt_str), "stop"))

    resolved = []
    dropped_unmatched_stops = 0
    dropped_unresolved_restarts = 0

    for permit, evs in events.items():
        evs.sort(key=lambda e: e[0])
        cur_start = pending.get(permit)
        for dt, kind in evs:
            if kind == "start":
                if cur_start is not None:
                    # Two starts with no stop between them -- data quirk at
                    # a chunk boundary or in the source itself. Keep the
                    # newer start; the earlier one is unresolvable.
                    dropped_unresolved_restarts += 1
                cur_start = dt
            else:
                if cur_start is not None:
                    resolved.append((permit, cur_start, dt))
                    cur_start = None
                else:
                    # A stop with no start in view -- its start happened
                    # before our backfill window began. Per the API's own
                    # docs, a Stop only ever follows a Start, so this is
                    # only possible right at our earliest boundary.
                    dropped_unmatched_stops += 1
        pending[permit] = cur_start

    with conn:
        for permit, start_dt, stop_dt in resolved:
            conn.execute(
                """
                INSERT INTO discharge_events (permit, start_utc, stop_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(permit, start_utc) DO UPDATE SET stop_utc = excluded.stop_utc
                """,
                (permit, start_dt.isoformat(), stop_dt.isoformat()),
            )
        for permit, start_dt in pending.items():
            if start_dt is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO discharge_events (permit, start_utc, stop_utc) VALUES (?, ?, NULL)",
                    (permit, start_dt.isoformat()),
                )

    return len(resolved), dropped_unmatched_stops, dropped_unresolved_restarts


def run_chunks(conn, pending, range_start, range_end, update_progress):
    chunk_start = range_start
    while chunk_start < range_end:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), range_end)
        params = {"dateStart": chunk_start.isoformat(), "dateEnd": chunk_end.isoformat()}

        t0 = time.time()
        starts, stops, total_pages = fetch_chunk(params)
        elapsed = time.time() - t0

        resolved_n, dropped_stops, dropped_restarts = process_chunk(conn, starts, stops, pending)
        if update_progress:
            save_progress(conn, chunk_end)

        still_open = sum(1 for v in pending.values() if v is not None)
        print(
            f"{chunk_start.isoformat()}..{chunk_end.isoformat()}: "
            f"{len(starts)} starts + {len(stops)} stops ({total_pages} pages, {elapsed:.1f}s) "
            f"-> {resolved_n} resolved, {still_open} still open"
            + (f", {dropped_stops} unmatched stops" if dropped_stops else "")
            + (f", {dropped_restarts} unresolved restarts" if dropped_restarts else "")
        )

        chunk_start = chunk_end
        # Adaptive politeness: more pages this chunk -> longer pause before
        # the next one, so a storm-heavy period doesn't get hammered with
        # back-to-back large requests.
        time.sleep(max(2.0, total_pages * 0.5))


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Backfill (or repair) national CSO discharge history.")
    parser.add_argument(
        "--repair", nargs=2, metavar=("START", "END"),
        help="Re-fetch and patch a specific YYYY-MM-DD date range without touching the "
             "overall resume checkpoint. Use this to fix a range an earlier run recorded "
             "as suspiciously empty.",
    )
    args = parser.parse_args()

    conn = db_connect()
    pending = load_pending(conn)

    if args.repair:
        range_start = date.fromisoformat(args.repair[0])
        range_end = date.fromisoformat(args.repair[1])
        print(f"Repairing {range_start.isoformat()}..{range_end.isoformat()} (resume checkpoint untouched)")
        try:
            run_chunks(conn, pending, range_start, range_end, update_progress=False)
        except KeyboardInterrupt:
            print("\nStopped early -- safe to re-run --repair for the same range any time.")
            return
        print("\nRepair pass complete.")
        return

    chunk_start = load_progress(conn)
    today = datetime.now(timezone.utc).date()

    if chunk_start >= today:
        print(f"Already caught up to {today.isoformat()}. Nothing to do.")
        return

    print(f"Resuming backfill from {chunk_start.isoformat()} toward {today.isoformat()}")
    print(f"({sum(1 for v in pending.values() if v is not None)} still-open interval(s) carried over from a previous run)")

    try:
        run_chunks(conn, pending, chunk_start, today, update_progress=True)
    except KeyboardInterrupt:
        print("\nStopped early. Progress was saved after every completed chunk -- re-run this script any time to continue.")
        return

    print(f"\nDone. Backfilled through {today.isoformat()}.")


if __name__ == "__main__":
    main()
