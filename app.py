from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from datetime import datetime, timezone, timedelta, date
from collections import defaultdict
from io import StringIO
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
import csv
import re
import requests
import threading
import traceback
import time
import math
import os

import db

import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import backfill_discharge_history as _discharge_backfill  # noqa: E402 -- shares fetch/pair/upsert logic with the auto-sync below

app = Flask(__name__)


@app.errorhandler(Exception)
def _log_unhandled_exception(e):
    """Log the full traceback of any unhandled exception to stdout so it
    shows up in the Render logs, instead of a bare 500 with no trace."""
    if isinstance(e, HTTPException):
        return e
    print(f"ERROR [unhandled] {request.method} {request.path}: {e!r}")
    traceback.print_exc()
    return "Internal Server Error", 500


# ---------------------------------------------------------------------------
# Thames Water Open Data API v2 — no key required.
#
# Unlike a curated-monitor dashboard, this app tracks every permit the API
# reports nationally. discharge/status is the source of truth for which
# permits exist right now (name, coordinates, receiving watercourse, live
# alert status); discharge/alerts supplies historic Start/Stop events used
# to compute discharge duration per window. Both are paginated at 200/page
# (larger page sizes have been observed to return HTTP 500) and self-throttled
# to roughly 1 request/second with retry-on-429 backoff, to stay a well-behaved
# client of a free public API now that every permit is being pulled instead of
# a shortlist of ~70.
# ---------------------------------------------------------------------------

_STATUS_URL  = "https://api.thameswater.co.uk/opendata/v2/discharge/status"
_ALERTS_URL  = "https://api.thameswater.co.uk/opendata/v2/discharge/alerts"
_PAGE_LIMIT  = 200

# A national Start+Stop pull is two full paginated sweeps of every permit in
# the country, but measured timing (see thames-water-api-diagnostic.yml) is
# ~7s for a 7-day window nationally, so a 30d window is not the bottleneck
# it was assumed to be.
_WINDOWS = [
    {"key": "24h", "label": "24 hours", "hours": 24},
    {"key": "7d",  "label": "7 days",   "hours": 168},
    {"key": "30d", "label": "30 days",  "hours": 720},
]
_DEFAULT_WINDOW   = "24h"
_LOOKBACK_DAYS    = 30

_cache          = {}
_cache_locks    = {}
_cache_locks_mu = threading.Lock()


def _get_lock(key):
    """Per-key lock, not one lock shared across every cache entry — a slow
    fetch for one key (e.g. the startup prewarm's national monitors pull,
    which can take several seconds) must not make a concurrent request for
    a completely different, not-yet-cached key (e.g. water_quality) fail to
    acquire and silently come back empty. That was happening in practice:
    the very first hit to a cold key while prewarm held the single shared
    lock always lost the race and got None back with no retry."""
    with _cache_locks_mu:
        if key not in _cache_locks:
            _cache_locks[key] = threading.Lock()
        return _cache_locks[key]


def get_cached(key, fetch_fn, ttl_seconds):
    now = datetime.now(timezone.utc).timestamp()
    if key in _cache and now - _cache[key]["ts"] < ttl_seconds:
        return _cache[key]["data"], _cache[key]["fetched_at"]

    # Non-blocking: if another thread is already mid-fetch for this same
    # key, serve whatever's cached instead of waiting on it. Blocking here
    # used to let gunicorn's worker-timeout kill the process mid-request
    # whenever a request landed during a slow prewarm of that same key —
    # this avoids the request thread ever blocking on a live network call.
    lock = _get_lock(key)
    if not lock.acquire(blocking=False):
        if key in _cache:
            return _cache[key]["data"], _cache[key]["fetched_at"]
        return None, ""

    try:
        now = datetime.now(timezone.utc).timestamp()
        if key in _cache and now - _cache[key]["ts"] < ttl_seconds:
            return _cache[key]["data"], _cache[key]["fetched_at"]
        try:
            data = fetch_fn()
            fetched_at = datetime.now(timezone.utc).strftime("%H:%M UTC")
            _cache[key] = {"ts": now, "data": data, "fetched_at": fetched_at}
            return data, fetched_at
        except Exception as e:
            print(f"Error fetching {key}: {e}")
            if key in _cache:
                return _cache[key]["data"], _cache[key]["fetched_at"]
            return None, ""
    finally:
        lock.release()


def _fetch_all_pages(url, params):
    """Paginate a Thames Water v2 endpoint at _PAGE_LIMIT/page, retrying
    3x on 429 with exponential backoff, pausing 1s between pages."""
    items = []
    offset = 0
    while True:
        page_params = dict(params, limit=_PAGE_LIMIT, offset=offset)
        for attempt in range(3):
            r = requests.get(
                url, params=page_params,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=20,
            )
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            break
        r.raise_for_status()
        page = r.json().get("items", [])
        items.extend(page)
        if len(page) < _PAGE_LIMIT:
            break
        offset += _PAGE_LIMIT
        time.sleep(1)
    return items


def _parse_dt(dt_str):
    """Parse a Thames Water API datetime, forcing UTC onto anything naive —
    some responses omit the offset entirely, which otherwise crashes later
    comparisons against an aware datetime."""
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _bng_to_wgs84(easting, northing):
    """British National Grid (OSGB36) easting/northing -> WGS84 lat/lon,
    for map pins from the x/y the API returns. Accurate to a few metres —
    fine for a map marker, not survey-grade. Returns None on failure."""
    try:
        a, b = 6377563.396, 6356256.909
        F0 = 0.9996012717
        lat0 = math.radians(49)
        lon0 = math.radians(-2)
        N0, E0 = -100000, 400000
        e2 = 1 - (b * b) / (a * a)
        n = (a - b) / (a + b)

        lat = lat0
        M = 0
        while True:
            lat = (northing - N0 - M) / (a * F0) + lat
            Ma = (1 + n + (5 / 4) * n**2 + (5 / 4) * n**3) * (lat - lat0)
            Mb = (3 * n + 3 * n**2 + (21 / 8) * n**3) * math.sin(lat - lat0) * math.cos(lat + lat0)
            Mc = ((15 / 8) * n**2 + (15 / 8) * n**3) * math.sin(2 * (lat - lat0)) * math.cos(2 * (lat + lat0))
            Md = (35 / 24) * n**3 * math.sin(3 * (lat - lat0)) * math.cos(3 * (lat + lat0))
            M = b * F0 * (Ma - Mb + Mc - Md)
            if abs(northing - N0 - M) < 0.00001:
                break

        sin_lat, cos_lat, tan_lat = math.sin(lat), math.cos(lat), math.tan(lat)
        nu = a * F0 / math.sqrt(1 - e2 * sin_lat**2)
        rho = a * F0 * (1 - e2) / (1 - e2 * sin_lat**2) ** 1.5
        eta2 = nu / rho - 1
        tan_lat2, tan_lat4, tan_lat6 = tan_lat**2, tan_lat**4, tan_lat**6

        VII = tan_lat / (2 * rho * nu)
        VIII = tan_lat / (24 * rho * nu**3) * (5 + 3 * tan_lat2 + eta2 - 9 * tan_lat2 * eta2)
        IX = tan_lat / (720 * rho * nu**5) * (61 + 90 * tan_lat2 + 45 * tan_lat4)
        X = 1 / (cos_lat * nu)
        XI = 1 / (cos_lat * 6 * nu**3) * (nu / rho + 2 * tan_lat2)
        XII = 1 / (cos_lat * 120 * nu**5) * (5 + 28 * tan_lat2 + 24 * tan_lat4)
        XIIA = 1 / (cos_lat * 5040 * nu**7) * (61 + 662 * tan_lat2 + 1320 * tan_lat4 + 720 * tan_lat6)

        dE = easting - E0
        lat_rad = lat - VII * dE**2 + VIII * dE**4 - IX * dE**6
        lon_rad = lon0 + X * dE - XI * dE**3 + XII * dE**5 - XIIA * dE**7
        return round(math.degrees(lat_rad), 6), round(math.degrees(lon_rad), 6)
    except Exception:
        return None


# Hammersmith Bridge — same reference point the sister frbc-tides project
# uses, so "upstream"/"downstream" and distance groupings on the monitors
# page are consistent with how that project already describes the river.
_HAMMERSMITH_LAT, _HAMMERSMITH_LON = 51.488, -0.224


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _fmt_hrs(seconds):
    if seconds <= 0:
        return "0h 00m"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m:02d}m"


def _fmt_last_discharge(info):
    if not info:
        return "No discharge in last 30 days"
    start_str = info["start"].strftime("%d %b %H:%M")
    if info["end"] is None:
        return f"Ongoing since {start_str} UTC"
    end_str = info["end"].strftime("%d %b %H:%M")
    return f"{start_str}–{end_str} UTC"


def _normalise_watercourse(name):
    """Group monitors by receiving watercourse for the summary/table views.
    Strips the tunnel-routing suffix so 'River Thames (via the Tideway
    tunnel)' and 'River Thames' land in the same group — tunnel connection
    is tracked as its own flag, not a separate watercourse."""
    if not name:
        return "Unknown"
    n = name.strip()
    lower = n.lower()
    idx = lower.find("(via")
    if idx != -1:
        n = n[:idx].strip()
    return n or "Unknown"


def get_all_monitors():
    """Full national pull of every permit discharge/status reports:
    permit, site name, coordinates, receiving watercourse, live alert
    status. This — not a hand-curated list — is the source of truth for
    which permits are tracked, since the point of this dashboard is
    everything the API knows about, not a hand-picked subset."""
    def fetch():
        t0 = time.monotonic()
        items = _fetch_all_pages(_STATUS_URL, {})
        print(f"discharge/status pull: {len(items)} items, {time.monotonic() - t0:.1f}s")
        monitors = {}
        for item in items:
            permit = item.get("permitNumber")
            if not permit:
                continue
            x, y = item.get("x"), item.get("y")
            latlon = _bng_to_wgs84(x, y) if x and y else None
            water = item.get("receivingWaterCourse") or ""
            monitors[permit] = {
                "permit":  permit,
                "name":    item.get("locationName") or permit,
                "water":   _normalise_watercourse(water),
                "tunnel_connected_inferred": "tideway tunnel" in water.lower(),
                "status":  item.get("alertStatus") or "Unknown",
                "lat":     latlon[0] if latlon else None,
                "lon":     latlon[1] if latlon else None,
            }
        return monitors
    return get_cached("all_monitors", fetch, ttl_seconds=1800)


def get_discharge_windows():
    """Historic Start/Stop events across every permit nationally, over the
    _LOOKBACK_DAYS window, reduced to discharge-seconds per permit per
    configured window. Mirrors the discharge/alerts fetch pattern proven
    in the sister frbc-tides project, generalised to no permit filter."""
    def fetch():
        t0 = time.monotonic()
        now_utc = datetime.now(timezone.utc)
        date_end = now_utc.strftime("%Y-%m-%d")
        date_start = (now_utc - timedelta(days=_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        params = {"dateStart": date_start, "dateEnd": date_end}

        starts = _fetch_all_pages(_ALERTS_URL, dict(params, alertType="Start"))
        time.sleep(1)
        stops = _fetch_all_pages(_ALERTS_URL, dict(params, alertType="Stop"))
        print(
            f"discharge/alerts pull ({date_start}..{date_end}): "
            f"{len(starts)} starts, {len(stops)} stops, {time.monotonic() - t0:.1f}s"
        )
        if starts:
            print(f"sample start permit: {starts[0].get('permitNumber')!r}")

        if not starts and not stops:
            # A 30-day national pull has never come back with genuinely zero
            # Start/Stop events (hundreds is typical) — treat an all-empty
            # result as a bad upstream response rather than caching it as
            # "every permit in the country stopped discharging", so
            # get_cached falls back to the last known-good data instead.
            raise RuntimeError(
                "discharge/alerts returned 0 starts and 0 stops nationally — "
                "treating as a bad response rather than genuine data"
            )

        stops_by_permit = defaultdict(list)
        for s in stops:
            permit, dt_str = s.get("permitNumber"), s.get("datetime")
            if permit and dt_str:
                try:
                    stops_by_permit[permit].append(_parse_dt(dt_str))
                except ValueError:
                    pass
        for lst in stops_by_permit.values():
            lst.sort()

        intervals = []
        for item in starts:
            permit, dt_str = item.get("permitNumber"), item.get("datetime")
            if not permit or not dt_str:
                continue
            try:
                start_dt = _parse_dt(dt_str)
            except ValueError:
                continue
            stop_dt = next((c for c in stops_by_permit.get(permit, []) if c >= start_dt), now_utc)
            if stop_dt > start_dt:
                intervals.append((permit, start_dt, stop_dt))

        def secs_for_window(hours):
            window_start = now_utc - timedelta(hours=hours)
            secs = defaultdict(float)
            for permit, start_dt, stop_dt in intervals:
                clipped_start = max(start_dt, window_start)
                clipped_stop = min(stop_dt, now_utc)
                if clipped_stop > clipped_start:
                    secs[permit] += (clipped_stop - clipped_start).total_seconds()
            return secs

        windows = {w["key"]: secs_for_window(w["hours"]) for w in _WINDOWS}

        # Most recent interval per permit within the lookback window, for map
        # hover detail. "end" is None while the discharge is still ongoing
        # (no matching Stop event found, so the interval was clipped to now).
        last_discharge = {}
        for permit, start_dt, stop_dt in intervals:
            prev = last_discharge.get(permit)
            if prev is None or start_dt > prev["start"]:
                last_discharge[permit] = {
                    "start": start_dt,
                    "end": None if stop_dt >= now_utc else stop_dt,
                }

        print(
            f"discharge windows computed: {len(intervals)} intervals, "
            + ", ".join(f"{k}={len(v)} permits" for k, v in windows.items())
        )
        return {"windows": windows, "last_discharge": last_discharge}

    return get_cached("discharge_windows", fetch, ttl_seconds=1800)


def build_dataset():
    monitors, monitors_fetched_at = get_all_monitors()
    windows_data, windows_fetched_at = get_discharge_windows()
    monitors = monitors or {}
    windows_data = windows_data or {"windows": {w["key"]: {} for w in _WINDOWS}, "last_discharge": {}}
    secs_by_window = windows_data["windows"]
    last_discharge = windows_data["last_discharge"]

    stations = []
    by_water_secs = defaultdict(lambda: {w["key"]: 0.0 for w in _WINDOWS})
    tunnel_secs = {w["key"]: 0.0 for w in _WINDOWS}
    total_secs = {w["key"]: 0.0 for w in _WINDOWS}
    total_discharging = 0

    for permit, m in monitors.items():
        secs = {w["key"]: secs_by_window.get(w["key"], {}).get(permit, 0) for w in _WINDOWS}
        is_discharging = m["status"].strip().lower() == "discharging"
        if is_discharging:
            total_discharging += 1

        # Tideway-tunnel-connected discharge is captured, not released to a
        # river, so it's tracked as its own bucket rather than folded into
        # the receiving watercourse's (or the national) totals.
        target = tunnel_secs if m["tunnel_connected_inferred"] else by_water_secs[m["water"]]
        if not m["tunnel_connected_inferred"]:
            for key in secs:
                total_secs[key] += secs[key]
        for key in secs:
            target[key] += secs[key]

        stations.append({
            **m,
            "hours_by_window": {k: _fmt_hrs(v) for k, v in secs.items()},
            "is_discharging": is_discharging,
            # Map colour category: tunnel-connected sites get their own
            # category regardless of nominal receiving watercourse, so
            # they're visually distinct rather than blending into "River
            # Thames" on the map.
            "map_category": "Tideway Tunnel" if m["tunnel_connected_inferred"] else m["water"],
            "last_discharge_str": _fmt_last_discharge(last_discharge.get(permit)),
        })

    stations.sort(key=lambda s: (not s["is_discharging"], s["name"]))

    non_tunnel_waterways = [
        {"name": name, "hours_by_window": {k: _fmt_hrs(v) for k, v in secs.items()}}
        for name, secs in sorted(
            by_water_secs.items(),
            key=lambda item: tuple(-item[1][w["key"]] for w in _WINDOWS),
        )
    ]
    thames_row = next((w for w in non_tunnel_waterways if w["name"] == "River Thames"), None)
    other_rows = [w for w in non_tunnel_waterways if w["name"] != "River Thames"]

    # Table row order: Tideway Tunnel (captured, own bucket) — Total
    # discharges into waterways (every non-tunnel permit, so Tideway Tunnel
    # is never double-counted here) — River Thames pulled out for
    # prominence — then remaining tributaries, busiest first.
    waterways = [
        {"name": "Tideway Tunnel", "hours_by_window": {k: _fmt_hrs(v) for k, v in tunnel_secs.items()}},
        {"name": "Total discharges into waterways", "hours_by_window": {k: _fmt_hrs(v) for k, v in total_secs.items()}},
    ]
    if thames_row:
        waterways.append(thames_row)
    waterways.extend(other_rows)

    water_quality, water_quality_fetched_at = get_water_quality()

    return {
        "stations":          stations,
        "waterways":         waterways,
        "windows":           _WINDOWS,
        "default_window":    _DEFAULT_WINDOW,
        "total_monitors":    len(stations),
        "total_discharging": total_discharging,
        "total_hours_by_window": {k: _fmt_hrs(v) for k, v in total_secs.items()},
        "monitors_fetched_at": monitors_fetched_at,
        "windows_fetched_at":  windows_fetched_at,
        "water_quality":            water_quality or {"frbc": None, "ptrc": None},
        "water_quality_fetched_at": water_quality_fetched_at,
    }


def build_monitor_groups(stations):
    """Group stations for the All Monitors page: Tideway Tunnel CSOs first,
    River Thames second, then each remaining tributary as its own group,
    ordered by distance from Hammersmith Bridge (nearest first) with an
    upstream/downstream subheading — determined by whether the tributary's
    nearest monitor sits west (upstream) or east (downstream) of Hammersmith's
    longitude, the same simplification the sister frbc-tides project uses
    for its own Thames-side grouping."""
    def sort_key(s):
        return (not s["is_discharging"], s["name"])

    tunnel = sorted((s for s in stations if s["tunnel_connected_inferred"]), key=sort_key)
    thames = sorted(
        (s for s in stations if not s["tunnel_connected_inferred"] and s["water"] == "River Thames"),
        key=sort_key,
    )

    other_by_water = defaultdict(list)
    for s in stations:
        if s["tunnel_connected_inferred"] or s["water"] == "River Thames":
            continue
        other_by_water[s["water"]].append(s)

    def nearest_and_distance(members):
        coord_members = [s for s in members if s["lat"] is not None and s["lon"] is not None]
        if not coord_members:
            return None, float("inf")
        nearest = min(
            coord_members,
            key=lambda s: _haversine_km(s["lat"], s["lon"], _HAMMERSMITH_LAT, _HAMMERSMITH_LON),
        )
        return nearest, _haversine_km(nearest["lat"], nearest["lon"], _HAMMERSMITH_LAT, _HAMMERSMITH_LON)

    tributary_groups = []
    for water, members in other_by_water.items():
        nearest, distance_km = nearest_and_distance(members)
        if nearest is None:
            subheading = "location unknown"
        elif nearest["lon"] < _HAMMERSMITH_LON:
            subheading = "upstream of Hammersmith"
        else:
            subheading = "downstream of Hammersmith"
        tributary_groups.append({
            "name": water,
            "subheading": subheading,
            "stations": sorted(members, key=sort_key),
            "_distance": distance_km,
        })
    tributary_groups.sort(key=lambda g: g["_distance"])
    for g in tributary_groups:
        del g["_distance"]

    groups = []
    if tunnel:
        groups.append({"name": "Tideway Tunnel CSO", "subheading": None, "stations": tunnel})
    if thames:
        groups.append({"name": "River Thames", "subheading": None, "stations": thames})
    groups.extend(tributary_groups)
    return groups


# ---------------------------------------------------------------------------
# Water quality — E. coli readings from participating testing sites'
# Google Sheets (FRBC / PTRC), same public-CSV-export data source and sheet
# layout already used by the sister frbc-tides project.
# ---------------------------------------------------------------------------

_WQ_FRBC_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1ZAzKgnACVxEM3j9eToxE9oAJpu6KZN0BNaeXd0jUmyM"
    "/export?format=csv&gid=1799951970"
)
_WQ_PTRC_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "14i4LMVw5OA1NvE8i14cbGo8M6nnUVlFV1pRbpjMmYnA"
    "/export?format=csv&gid=132413204"
)
_WQ_ECOLI_GOOD     = 1_000    # CFU/100ml — at/below this is shown as good (green)
_WQ_STALE_DAYS     = 7
_WQ_HISTORY_DAYS   = 365      # Testing page chart window
_WQ_REFRESH_SECONDS = 86400   # daily — the sheets are hand-updated ~weekly, so
                               # there's nothing to gain from polling more often
                               # than that, and it keeps this to one small CSV
                               # fetch/day per site once running on the Pi.

_WQ_VOID_TOKENS = ("", "void", "na", "n/a", "-", "tbc", "pending", "error", "n/k", "unknown")

_WQ_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "water_quality.db")


def _wq_parse_ecoli(raw):
    """Extract an integer CFU/100ml reading from a messy sheet cell. Handles
    plain numbers, comma-thousands ("43,000"), a value in parentheses
    (seen alongside a separate flag/status in the same cell), and the usual
    non-numeric placeholders ("void", "TBC", blank, etc.)."""
    if raw is None:
        return None
    raw = str(raw).strip()
    if raw.lower() in _WQ_VOID_TOKENS:
        return None
    m = re.search(r"\((\d[\d,]*)\)", raw)
    if m:
        return int(m.group(1).replace(",", ""))
    d = re.search(r"[\d,]+", raw)
    if d:
        try:
            return int(d.group(0).replace(",", ""))
        except ValueError:
            return None
    return None


def _wq_parse_date(raw):
    """Parse a sheet date cell. Sheets exported to CSV can render dates in
    whatever display format the cell has (UK day/month/year is what both
    known sources use), sometimes with a trailing time component — take
    the date portion before any whitespace and try each known format."""
    if not raw:
        return None
    raw = str(raw).strip().split(" ")[0]
    if not raw:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _wq_risk(ecoli_value, stale=False):
    if stale or ecoli_value is None:
        return "unknown"
    return "good" if ecoli_value <= _WQ_ECOLI_GOOD else "poor"


def _wq_find_col(keys, *candidates):
    for c in candidates:
        for k in keys:
            if c.lower() in k.lower():
                return k
    return None


def _wq_find_ecoli_col(keys, site_label):
    """Find the E. coli reading column by keyword rather than an exact
    name, since the two known sources don't share a header ("Reading
    E.Coli/100ml" vs "Alert One E.Coli reading (CFU per 100ml)"), and a
    future source may word it differently again. Columns that are clearly a
    secondary/additional monitor reading are excluded. If more than one
    candidate remains, the first is used but all candidates are logged so a
    genuinely ambiguous sheet is at least visible in the logs rather than
    silently guessed."""
    candidates = []
    for k in keys:
        kl = k.lower()
        if "e.coli" in kl or "ecoli" in kl or "e coli" in kl or "alert one" in kl:
            if "additional" not in kl and "monitor 2" not in kl and "monitor 3" not in kl:
                candidates.append(k)
    if not candidates:
        print(f"WARNING [wq/{site_label}]: no E. coli column found among headers {keys!r}")
        return None
    if len(candidates) > 1:
        print(f"WARNING [wq/{site_label}]: multiple possible E. coli columns {candidates!r}, using {candidates[0]!r}")
    return candidates[0]


def _wq_parse_sheet(raw_csv, site_label):
    """Parse a testing site's Google Sheet export into a full, date-sorted
    reading history. Column detection (rather than a fixed column name)
    because the sheets don't share exactly the same headers, and sheet
    owners can rename/reorder columns at any time."""
    reader = csv.DictReader(StringIO(raw_csv))
    rows = [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in reader]
    if not rows:
        print(f"WARNING [wq/{site_label}]: sheet had no data rows")
        return []
    keys = list(rows[0].keys())

    ecoli_col = _wq_find_ecoli_col(keys, site_label)
    if not ecoli_col:
        return []

    date_col = _wq_find_col(keys, "sample date", "date")
    if not date_col:
        print(f"WARNING [wq/{site_label}]: no date column found among headers {keys!r}")

    results = []
    for row in rows:
        raw_value = row.get(ecoli_col, "")
        ecoli_val = _wq_parse_ecoli(raw_value)
        sample_date = _wq_parse_date(row.get(date_col, "") if date_col else "")
        if sample_date is None:
            continue   # no date to key this reading on — can't chart or store it
        d_ago = (date.today() - sample_date).days
        stale = d_ago > _WQ_STALE_DAYS
        results.append({
            "date":          sample_date.isoformat(),
            "date_str":      sample_date.strftime("%-d %b %Y"),
            "days_ago":      d_ago,
            "stale":         stale,
            "ecoli":         ecoli_val,
            "risk":          _wq_risk(ecoli_val, stale=stale),
            "raw_value":     raw_value,
            "source_column": ecoli_col,
        })
    results.sort(key=lambda r: r["date"], reverse=True)
    print(
        f"INFO [wq/{site_label}]: parsed {len(results)} dated rows "
        f"({sum(1 for r in results if r['ecoli'] is not None)} with a reading), "
        f"ecoli_col={ecoli_col!r} date_col={date_col!r}"
    )
    return results


def _wq_fetch_site(url, site_label):
    if not url:
        return []
    # Some hosts (Google included) treat a bare urllib request differently
    # from a browser — a real User-Agent avoids that class of surprise.
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=15) as resp:
            return _wq_parse_sheet(resp.read().decode("utf-8"), site_label)
    except HTTPError as e:
        if e.code == 403:
            # Sheet genuinely not public — real "no data", not transient.
            print(f"INFO [wq/{site_label}]: sheet not public (403)")
            return []
        print(f"ERROR [wq/{site_label}]: HTTP {e.code} {e.reason}")
        raise
    except URLError as e:
        print(f"ERROR [wq/{site_label}]: {e}")
        raise


def _wq_db_connect():
    conn = db.connect(_WQ_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ecoli_readings (
            site           TEXT NOT NULL,
            sample_date    TEXT NOT NULL,
            ecoli_cfu      INTEGER,
            raw_value      TEXT,
            source_column  TEXT,
            first_seen_at  TEXT NOT NULL,
            last_seen_at   TEXT NOT NULL,
            PRIMARY KEY (site, sample_date)
        )
    """)
    return conn


def _wq_db_upsert(site, rows):
    """Persist newly parsed rows into the local database. Never deletes —
    if the source sheet is later edited or a row disappears, our copy of
    that reading is kept, which is the reason this store exists at all.
    A re-fetch that includes a date we already have updates the value
    (the sheet owner corrected something) without losing first_seen_at."""
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn = _wq_db_connect()
    try:
        with conn:
            for r in rows:
                conn.execute(
                    """
                    INSERT INTO ecoli_readings
                        (site, sample_date, ecoli_cfu, raw_value, source_column, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(site, sample_date) DO UPDATE SET
                        ecoli_cfu     = excluded.ecoli_cfu,
                        raw_value     = excluded.raw_value,
                        source_column = excluded.source_column,
                        last_seen_at  = excluded.last_seen_at
                    """,
                    (site, r["date"], r["ecoli"], r["raw_value"], r["source_column"], now, now),
                )
    finally:
        conn.close()


def _wq_db_read_history(site):
    """Read this site's full stored history back out, in the same shape
    _wq_parse_sheet produces, so callers don't care whether a value came
    from a fresh fetch or from what we'd already saved."""
    conn = _wq_db_connect()
    try:
        cur = conn.execute(
            "SELECT sample_date, ecoli_cfu FROM ecoli_readings WHERE site = ? ORDER BY sample_date DESC",
            (site,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    results = []
    for sample_date_str, ecoli_val in rows:
        sample_date = date.fromisoformat(sample_date_str)
        d_ago = (date.today() - sample_date).days
        stale = d_ago > _WQ_STALE_DAYS
        results.append({
            "date":     sample_date_str,
            "date_str": sample_date.strftime("%-d %b %Y"),
            "days_ago": d_ago,
            "stale":    stale,
            "ecoli":    ecoli_val,
            "risk":     _wq_risk(ecoli_val, stale=stale),
        })
    return results


def get_water_quality():
    """E. coli readings for each testing site. Cached per _WQ_REFRESH_SECONDS.
    Each site's live sheet is fetched best-effort and upserted into a local
    SQLite database; the history and latest-reading summary returned to
    callers is always read back from that database, not the raw fetch, so a
    transient network failure, a 403, or the source sheet being edited or
    deleted only means no new rows this cycle — nothing already captured is
    lost, and the two sites' fates aren't tied together."""
    def fetch_one(site_key, label, url):
        try:
            rows = _wq_fetch_site(url, label)
            _wq_db_upsert(site_key, rows)
        except Exception as e:
            print(f"ERROR [wq/{label}]: live fetch failed, serving stored history only: {e!r}")

        history = _wq_db_read_history(site_key)
        latest = next((r for r in history if r["ecoli"] is not None), None)
        if latest:
            d = latest["days_ago"]
            if d == 0:
                days_str = "today"
            elif d == 1:
                days_str = "yesterday"
            elif d is not None:
                days_str = f"{d} days ago"
            else:
                days_str = "date unknown"
            latest_summary = {
                "ecoli_str":    f"{latest['ecoli']:,}",
                "date_str":     latest["date_str"],
                "days_ago_str": days_str,
                "risk":         latest["risk"],
                "available":    True,
            }
        else:
            latest_summary = {
                "ecoli_str": "—", "date_str": "—", "days_ago_str": "unavailable",
                "risk": "unknown", "available": False,
            }
        return {"history": history, "latest": latest_summary}

    def fetch():
        return {
            "frbc": fetch_one("frbc", "FRBC", _WQ_FRBC_URL),
            "ptrc": fetch_one("ptrc", "PTRC", _WQ_PTRC_URL),
        }

    return get_cached("water_quality", fetch, ttl_seconds=_WQ_REFRESH_SECONDS)


# ---------------------------------------------------------------------------
# Total discharge overlay (Testing page) — reads the raw event history
# backfilled by scripts/backfill_discharge_history.py, aggregated into the
# same weekly buckets as the E. coli chart so both can share one x-axis.
# This only reads what's already in discharge_history.db/Turso; nothing in
# the live app writes to it, so it reflects whatever the backfill/repair
# script last captured, not a live-updating feed.
# ---------------------------------------------------------------------------

_DISCHARGE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "discharge_history.db")


def _week_buckets(range_start=None, range_end=None):
    """Weekly (start, end) date pairs, end exclusive, oldest first, spanning
    [range_start, range_end]. Defaults to the last _WQ_HISTORY_DAYS days
    ending today when no range is given. The final bucket's end is always
    one day past range_end -- with an exclusive end date, ending exactly on
    the last day would put that day's own events outside every bucket,
    silently dropping the most current data when range_end is today."""
    if range_end is None:
        range_end = date.today()
    if range_start is None:
        range_start = range_end - timedelta(days=_WQ_HISTORY_DAYS)

    end = range_end + timedelta(days=1)
    buckets = []
    while end > range_start:
        start = max(range_start, end - timedelta(days=7))
        buckets.append((start, end))
        end = start
    buckets.reverse()
    return buckets


def _load_discharge_intervals():
    conn = db.connect(_DISCHARGE_DB_PATH, env_prefix="TURSO_DISCHARGE")
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discharge_events (
                permit      TEXT NOT NULL,
                start_utc   TEXT NOT NULL,
                stop_utc    TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (permit, start_utc)
            )
        """)
        rows = conn.execute("SELECT permit, start_utc, stop_utc FROM discharge_events").fetchall()
    finally:
        conn.close()

    intervals = []
    for permit, start_utc, stop_utc in rows:
        try:
            start_dt = _parse_dt(start_utc)
            stop_dt = _parse_dt(stop_utc) if stop_utc else None
        except ValueError:
            continue
        intervals.append((permit, start_dt, stop_dt))
    return intervals


# ---------------------------------------------------------------------------
# Discharge history auto-sync -- makes discharge_history.db self-updating
# instead of depending on someone manually re-running the backfill script.
# Runs as a background thread (same pattern as _prewarm), reusing the
# fetch/pair/upsert logic from scripts/backfill_discharge_history.py so
# there's one implementation of "how to safely pull and store this data",
# not two.
#
# Deliberately separate from that script's own backfill_progress checkpoint:
# this only ever does short, recent catch-ups (a day or two), never the
# full multi-year historical crawl -- that stays a deliberate, manual,
# one-off action. A fresh environment with no prior sync history (e.g. a
# first deploy on Render) should not silently kick off an hours-long
# historical pull in the background.
#
# API-behaviour notes this respects:
#   - Docs: new data appears roughly every 30 minutes, with per-sensor and
#     processing delay meaning a discharge is normally visible within an
#     hour of starting. Catching up from (last_synced - 1 day) is generous
#     headroom against that, and syncing every 30 min matches the API's own
#     update cadence -- no point polling faster than the source refreshes.
#   - Docs: 5 requests/second/user is the documented (not yet enforced)
#     rate limit. A sync cycle is normally 1-2 short chunks plus a bounded
#     handful of unclosed-record rechecks -- comparable to the live app's
#     existing 30-day discharge_windows pull, not an additional heavy load.
# ---------------------------------------------------------------------------

_DISCHARGE_AUTO_SYNC_ENABLED = os.environ.get("DISCHARGE_AUTO_SYNC", "1") != "0"
_DISCHARGE_SYNC_INTERVAL_SECONDS = 1800   # matches the API's own ~30 min update cadence
_DISCHARGE_SYNC_CATCHUP_BUFFER_DAYS = 1   # re-check the day before "last synced" too, for late-arriving data
_DISCHARGE_UNCLOSED_MAX_RETRIES = 5       # give up on any single record after this many failed resolve attempts
_DISCHARGE_UNCLOSED_BATCH_LIMIT = 20      # cap on how many still-open records get rechecked per sync cycle
_DISCHARGE_UNCLOSED_MIN_AGE_DAYS = 2      # only recheck records old enough that "still discharging" is implausible


def _ensure_sync_schema(conn):
    """Add what the auto-sync needs on top of the backfill script's base
    schema: a retry counter per event (so a permanently-unresolvable record
    stops being retried forever) and its own progress checkpoint, kept
    separate from backfill_progress so the two never interact."""
    try:
        conn.execute("ALTER TABLE discharge_events ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass   # column already exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discharge_sync_progress (
            id           INTEGER PRIMARY KEY CHECK (id = 0),
            last_synced  TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        )
    """)


def _load_sync_progress(conn):
    row = conn.execute("SELECT last_synced FROM discharge_sync_progress WHERE id = 0").fetchone()
    if row:
        return date.fromisoformat(row[0])
    # No prior auto-sync yet -- start from a conservative recent window,
    # not the full historical range. The one-off manual backfill script is
    # what's meant to establish deep history; this only ever keeps it current.
    return datetime.now(timezone.utc).date() - timedelta(days=_DISCHARGE_SYNC_CATCHUP_BUFFER_DAYS)


def _save_sync_progress(conn, synced_through):
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO discharge_sync_progress (id, last_synced, updated_at)
            VALUES (0, ?, ?)
            ON CONFLICT(id) DO UPDATE SET last_synced = excluded.last_synced, updated_at = excluded.updated_at
            """,
            (synced_through.isoformat(), now),
        )


def _retry_unclosed_discharge_records(conn, pending):
    """Bounded attempt to close out long-open records: a permit with no
    Stop event yet is either genuinely still discharging (recent -- left
    alone) or sitting on a gap in the source data (old -- worth another
    try, but not forever). Two independent caps, per the requirement that
    this must not turn into an unbounded loop: a per-record retry_count
    ceiling, and a per-cycle limit on how many records get attempted at
    all."""
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=_DISCHARGE_UNCLOSED_MIN_AGE_DAYS)).isoformat()
    rows = conn.execute(
        """
        SELECT permit, start_utc FROM discharge_events
        WHERE stop_utc IS NULL AND start_utc < ? AND retry_count < ?
        ORDER BY start_utc ASC LIMIT ?
        """,
        (cutoff, _DISCHARGE_UNCLOSED_MAX_RETRIES, _DISCHARGE_UNCLOSED_BATCH_LIMIT),
    ).fetchall()

    today = datetime.now(timezone.utc).date()
    for permit, start_utc in rows:
        start_date = datetime.fromisoformat(start_utc).date()
        window_start = start_date - timedelta(days=1)
        window_end = min(start_date + timedelta(days=14), today)
        if window_end <= window_start:
            continue

        _discharge_backfill.run_chunks(conn, pending, window_start, window_end, update_progress=False)

        still_open = conn.execute(
            "SELECT 1 FROM discharge_events WHERE permit = ? AND start_utc = ? AND stop_utc IS NULL",
            (permit, start_utc),
        ).fetchone()
        if still_open:
            with conn:
                conn.execute(
                    "UPDATE discharge_events SET retry_count = retry_count + 1 WHERE permit = ? AND start_utc = ?",
                    (permit, start_utc),
                )


def _sync_discharge_history_once():
    conn = _discharge_backfill.db_connect()
    try:
        _ensure_sync_schema(conn)
        pending = _discharge_backfill.load_pending(conn)
        today = datetime.now(timezone.utc).date()

        last_synced = _load_sync_progress(conn)
        catchup_start = max(_discharge_backfill.GO_LIVE_DATE, last_synced - timedelta(days=_DISCHARGE_SYNC_CATCHUP_BUFFER_DAYS))
        if catchup_start < today:
            print(f"INFO [discharge-sync]: catching up {catchup_start.isoformat()}..{today.isoformat()}")
            _discharge_backfill.run_chunks(conn, pending, catchup_start, today, update_progress=False)
            _save_sync_progress(conn, today)

        _retry_unclosed_discharge_records(conn, pending)
    finally:
        conn.close()


def _discharge_history_sync_loop():
    while True:
        try:
            _sync_discharge_history_once()
        except Exception as e:
            print(f"ERROR [discharge-sync]: {e!r}")
        time.sleep(_DISCHARGE_SYNC_INTERVAL_SECONDS)


def get_unclosed_discharge_report():
    """Discharge events with no Stop event recorded, old enough that "still
    discharging" is implausible (same cutoff the auto-sync retry uses) --
    these are most likely gaps in Thames Water's own published data rather
    than genuinely ongoing discharges. Surfaced as a Summary page warning
    and a full report formatted to paste into an email to Thames Water."""
    def fetch():
        conn = _discharge_backfill.db_connect()
        try:
            cutoff = (datetime.now(timezone.utc).date() - timedelta(days=_DISCHARGE_UNCLOSED_MIN_AGE_DAYS)).isoformat()
            rows = conn.execute(
                """
                SELECT permit, start_utc, retry_count FROM discharge_events
                WHERE stop_utc IS NULL AND start_utc < ?
                ORDER BY start_utc ASC
                """,
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()

        monitors, _ = get_all_monitors()
        monitors = monitors or {}
        now_utc = datetime.now(timezone.utc)

        report = []
        for permit, start_utc, retry_count in rows:
            start_dt = _parse_dt(start_utc)
            m = monitors.get(permit)
            report.append({
                "permit":      permit,
                "name":        m["name"] if m else "Unknown (not in current monitor list)",
                "water":       m["water"] if m else "Unknown",
                "start_str":   start_dt.strftime("%Y-%m-%d %H:%M"),
                "days_open":   (now_utc - start_dt).days,
                "retry_count": retry_count,
            })
        return report

    return get_cached("unclosed_discharge_report", fetch, ttl_seconds=_WQ_REFRESH_SECONDS)


def _format_unclosed_report_text(report, fetched_at):
    lines = [
        "Discharge event data query - missing Stop records",
        "",
        "The following permitted discharge monitors show a Start event with no "
        "matching Stop event in the Thames Water Discharge to Environment (DTE) "
        "Open Data API discharge/alerts feed, despite enough time having passed "
        "that the discharge is unlikely to still be ongoing. Could you confirm "
        "whether these are genuine ongoing discharges, or a gap in the published data?",
        "",
        f"{'Permit':<15}{'Site name':<35}{'Watercourse':<20}{'Start (UTC)':<18}{'Days open':<11}{'Retries'}",
    ]
    for r in report:
        lines.append(
            f"{r['permit']:<15}{r['name'][:33]:<35}{r['water'][:18]:<20}{r['start_str']:<18}{r['days_open']:<11}{r['retry_count']}"
        )
    lines.append("")
    lines.append(f"Generated {fetched_at or datetime.now(timezone.utc).strftime('%H:%M UTC')} from the Thames Water Open Data API (discharge/alerts), via the Thames Water CSO Dashboard.")
    return "\n".join(lines)


_DISCHARGE_ZONE_LIMIT = 15   # cap on individual watercourse toggle options, busiest first
_DISCHARGE_OPEN_MAX_ASSUMED_HOURS = 48   # cap on how long a still-open (no Stop yet) record
                                          # is assumed to keep discharging; matches the ~2-day
                                          # threshold used elsewhere for "too old to plausibly
                                          # still be ongoing" (_DISCHARGE_UNCLOSED_MIN_AGE_DAYS)


def get_total_discharge_weekly(range_start=None, range_end=None):
    """Weekly discharge hours per zone, always excluding Tideway Tunnel
    permits (captured, never reaches a river) -- same exclusion the
    Summary page's subtotal already applies. Returns a "National (excl.
    Tideway Tunnel)" total plus the busiest individual watercourses as
    separate toggleable series, matching the receiving-watercourse
    breakdown the Summary page already shows -- since National mixes in
    every river in the country, it's on a totally different scale to any
    single watercourse (thousands of hours/week vs a handful), which makes
    them unreadable plotted together; letting the page pick one zone at a
    time keeps the chart's axis meaningful for whichever is selected.

    range_start/range_end select an arbitrary date window (e.g. from the
    Testing page's year/month picker) instead of the default last-12-months;
    cached per distinct range since different windows are genuinely
    different results, not the same cache entry.

    An unresolved record (no Stop event yet) is only ever assumed to run for
    up to _DISCHARGE_OPEN_MAX_ASSUMED_HOURS from its start, never all the
    way to "now" -- extending an old, never-closed Start indefinitely would
    treat what's almost certainly a missing-Stop data gap (see the Summary
    page's unclosed-discharge warning) as a discharge that's been running
    continuously for months or years, which silently corrupts every week
    bucket in between with an inflated, flat-lined total.

    Watercourse classification comes from the *current* monitor list, since
    it's a fixed site property, not something that varies week to week; a
    permit retired before today's monitor list won't be classifiable and
    is excluded from every zone (including National). "River Thames" is
    always included as its own zone regardless of the busiest-N cut, since
    it's the default comparison for these Thames-side testing sites and
    shouldn't silently disappear if a given period's individual tributary
    totals happen to outrank it."""
    buckets = _week_buckets(range_start, range_end)
    cache_key = f"total_discharge_weekly:{buckets[0][0].isoformat()}:{buckets[-1][1].isoformat()}"

    def fetch():
        monitors, _ = get_all_monitors()
        monitors = monitors or {}

        def canonical_zone(water_name):
            # Unify any spelling/case variant ("River Thames", "the Thames",
            # "River Thames Tideway", ...) into one zone -- without this, a
            # handful of small variant-named groups could each be too small
            # to make the busiest-N cut individually, and the exact string
            # "River Thames" the frontend defaults to might not exist at all.
            if water_name and "thames" in water_name.lower():
                return "River Thames"
            return water_name or "Unknown"

        permit_zone = {
            p: canonical_zone(m.get("water"))
            for p, m in monitors.items()
            if not m.get("tunnel_connected_inferred")
        }

        intervals = _load_discharge_intervals()
        now_utc = datetime.now(timezone.utc)
        n = len(buckets)
        window_start = datetime.combine(buckets[0][0], datetime.min.time(), tzinfo=timezone.utc)

        national_secs = [0.0] * n
        secs_by_zone = defaultdict(lambda: [0.0] * n)

        for permit, start_dt, stop_dt in intervals:
            zone = permit_zone.get(permit)
            if zone is None:
                continue   # tunnel-connected, or not in the current monitor list
            if stop_dt is not None:
                effective_stop = stop_dt
            else:
                assumed_stop = start_dt + timedelta(hours=_DISCHARGE_OPEN_MAX_ASSUMED_HOURS)
                effective_stop = min(assumed_stop, now_utc)
            if effective_stop <= window_start:
                continue
            start_idx = max(0, (start_dt.date() - buckets[0][0]).days // 7)
            for i in range(start_idx, n):
                b_start = datetime.combine(buckets[i][0], datetime.min.time(), tzinfo=timezone.utc)
                b_end = datetime.combine(buckets[i][1], datetime.min.time(), tzinfo=timezone.utc)
                if b_start >= effective_stop:
                    break
                clipped_start = max(start_dt, b_start)
                clipped_stop = min(effective_stop, b_end)
                if clipped_stop > clipped_start:
                    secs = (clipped_stop - clipped_start).total_seconds()
                    national_secs[i] += secs
                    secs_by_zone[zone][i] += secs

        busiest = sorted(secs_by_zone.items(), key=lambda kv: -sum(kv[1]))[:_DISCHARGE_ZONE_LIMIT]

        zones = {"National (excl. Tideway Tunnel)": [round(s / 3600, 2) for s in national_secs]}
        for name, secs in busiest:
            zones[name] = [round(s / 3600, 2) for s in secs]
        if "River Thames" in secs_by_zone and "River Thames" not in zones:
            zones["River Thames"] = [round(s / 3600, 2) for s in secs_by_zone["River Thames"]]

        return {
            "week_starts": [b[0].isoformat() for b in buckets],
            "zones":       zones,
        }

    return get_cached(cache_key, fetch, ttl_seconds=_WQ_REFRESH_SECONDS)


def _bucket_ecoli_readings(history, buckets):
    """Align a site's E. coli readings onto the same weekly buckets used for
    the discharge overlay, so both series share one x-axis. Readings are
    normally about weekly already, so this is mostly a 1:1 mapping; a week
    with no reading gets None (a gap in the line, not a zero)."""
    values = [None] * len(buckets)
    for r in history:
        if r["ecoli"] is None or not r["date"]:
            continue
        d = date.fromisoformat(r["date"])
        for i, (b_start, b_end) in enumerate(buckets):
            if b_start <= d < b_end:
                values[i] = r["ecoli"]
                break
    return values


@app.route("/")
def index():
    data = build_dataset()
    unclosed_report, _ = get_unclosed_discharge_report()
    data["unclosed_count"] = len(unclosed_report or [])
    return render_template("index.html", **data)


@app.route("/unclosed-discharges")
def unclosed_discharges_view():
    report, fetched_at = get_unclosed_discharge_report()
    report = report or []
    report_text = _format_unclosed_report_text(report, fetched_at)
    return render_template(
        "unclosed_discharges.html",
        report=report,
        fetched_at=fetched_at,
        report_text=report_text,
        min_age_days=_DISCHARGE_UNCLOSED_MIN_AGE_DAYS,
    )


@app.route("/map")
def map_view():
    data = build_dataset()
    return render_template("map.html", **data)


@app.route("/monitors")
def monitors_view():
    data = build_dataset()
    data["groups"] = build_monitor_groups(data["stations"])
    return render_template("monitors.html", **data)


_MONTH_NAMES = [
    (1, "Jan"), (2, "Feb"), (3, "Mar"), (4, "Apr"), (5, "May"), (6, "Jun"),
    (7, "Jul"), (8, "Aug"), (9, "Sep"), (10, "Oct"), (11, "Nov"), (12, "Dec"),
]
_TESTING_EARLIEST_YEAR = 2022   # discharge history only goes back to late Dec 2022


def _month_bounds(year, month):
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end - timedelta(days=1)


def _parse_testing_range():
    """Read from_year/from_month/to_year/to_month query params (the Testing
    page's date range picker) into an actual (start, end) date range,
    defaulting to the last _WQ_HISTORY_DAYS days ending today when a param
    is absent or invalid. Clamps the end to today and swaps the bounds if
    from ends up after to, rather than erroring on a nonsensical range."""
    today = date.today()
    default_start = today - timedelta(days=_WQ_HISTORY_DAYS)

    try:
        range_start, _ = _month_bounds(
            int(request.args.get("from_year", default_start.year)),
            int(request.args.get("from_month", default_start.month)),
        )
    except (ValueError, TypeError):
        range_start = default_start

    try:
        _, range_end = _month_bounds(
            int(request.args.get("to_year", today.year)),
            int(request.args.get("to_month", today.month)),
        )
    except (ValueError, TypeError):
        range_end = today

    range_end = min(range_end, today)
    if range_start > range_end:
        range_start, range_end = range_end, range_start
    return range_start, range_end


@app.route("/testing")
def testing_view():
    water_quality, water_quality_fetched_at = get_water_quality()
    water_quality = water_quality or {"frbc": None, "ptrc": None}

    range_start, range_end = _parse_testing_range()
    buckets = _week_buckets(range_start, range_end)
    spans_multiple_years = range_start.year != range_end.year
    week_labels = [
        b[0].strftime("%-d %b %Y") if spans_multiple_years else b[0].strftime("%-d %b")
        for b in buckets
    ]

    discharge_weekly, discharge_fetched_at = get_total_discharge_weekly(range_start, range_end)
    discharge_zones = (discharge_weekly or {}).get("zones") or {}

    chart_data = {}
    chart_has_data = {}
    for key, site in water_quality.items():
        values = _bucket_ecoli_readings(site["history"], buckets) if site else [None] * len(buckets)
        chart_data[key] = values
        chart_has_data[key] = any(v is not None for v in values)

    return render_template(
        "testing.html",
        water_quality=water_quality,
        water_quality_fetched_at=water_quality_fetched_at,
        week_labels=week_labels,
        chart_data=chart_data,
        chart_has_data=chart_has_data,
        discharge_zones=discharge_zones,
        discharge_fetched_at=discharge_fetched_at,
        range_start=range_start,
        range_end=range_end,
        month_names=_MONTH_NAMES,
        year_options=list(range(_TESTING_EARLIEST_YEAR, date.today().year + 1)),
    )


@app.route("/data")
def data_endpoint():
    return jsonify(build_dataset())


@app.route("/ping")
def ping():
    return "ok"


@app.route("/debug-cache")
def debug_cache():
    """Temporary diagnostic: dump the raw in-memory cache state directly,
    bypassing build_dataset(), to see whether _cache itself is populated
    or whether the bug is downstream of it."""
    now = datetime.now(timezone.utc).timestamp()
    return jsonify({
        "pid": os.getpid(),
        "cache_keys": list(_cache.keys()),
        "locked_keys": [k for k, l in _cache_locks.items() if l.locked()],
        "cache_detail": {
            k: {
                "age_seconds": round(now - v["ts"], 1),
                "fetched_at": v["fetched_at"],
                "data_len": len(v["data"]) if hasattr(v["data"], "__len__") else repr(v["data"])[:100],
            }
            for k, v in _cache.items()
        },
    })


def _prewarm():
    print("Pre-warming national CSO cache on startup...")
    try:
        get_all_monitors()
    except Exception as e:
        print(f"Pre-warm error [monitors]: {e!r}")
    try:
        get_discharge_windows()
    except Exception as e:
        print(f"Pre-warm error [discharge_windows]: {e!r}")


threading.Thread(target=_prewarm, daemon=True).start()

if _DISCHARGE_AUTO_SYNC_ENABLED:
    threading.Thread(target=_discharge_history_sync_loop, daemon=True).start()


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1")
