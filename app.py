from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import requests
import threading
import traceback
import time
import math
import os

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
_cache_lock     = threading.Lock()


def get_cached(key, fetch_fn, ttl_seconds):
    now = datetime.now(timezone.utc).timestamp()
    if key in _cache and now - _cache[key]["ts"] < ttl_seconds:
        return _cache[key]["data"], _cache[key]["fetched_at"]

    # Non-blocking: if another thread (typically the startup prewarm) is
    # already mid-fetch, serve whatever's cached instead of waiting on it.
    # Blocking here used to let gunicorn's worker-timeout kill the process
    # mid-request whenever a request landed during a slow prewarm, which
    # then made the *next* worker's very first fetch look suspiciously
    # unreliable too — this avoids the request thread ever blocking on a
    # live network call at all.
    if not _cache_lock.acquire(blocking=False):
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
        _cache_lock.release()


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


def _fmt_hrs(seconds):
    if seconds <= 0:
        return "0h 00m"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m:02d}m"


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
        print(
            f"discharge windows computed: {len(intervals)} intervals, "
            + ", ".join(f"{k}={len(v)} permits" for k, v in windows.items())
        )
        return windows

    return get_cached("discharge_windows", fetch, ttl_seconds=1800)


def build_dataset():
    monitors, monitors_fetched_at = get_all_monitors()
    secs_by_window, windows_fetched_at = get_discharge_windows()
    monitors = monitors or {}
    secs_by_window = secs_by_window or {w["key"]: {} for w in _WINDOWS}

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
        })

    stations.sort(key=lambda s: (not s["is_discharging"], s["name"]))

    waterways = [
        {"name": name, "hours_by_window": {k: _fmt_hrs(v) for k, v in secs.items()}}
        for name, secs in sorted(
            by_water_secs.items(),
            key=lambda item: tuple(-item[1][w["key"]] for w in _WINDOWS),
        )
    ]
    waterways.insert(0, {
        "name": "Tideway Tunnel",
        "hours_by_window": {k: _fmt_hrs(v) for k, v in tunnel_secs.items()},
    })

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
    }


@app.route("/")
def index():
    data = build_dataset()
    return render_template("index.html", **data)


@app.route("/map")
def map_view():
    data = build_dataset()
    return render_template("map.html", **data)


@app.route("/monitors")
def monitors_view():
    data = build_dataset()
    return render_template("monitors.html", **data)


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
    lock_acquired = _cache_lock.acquire(blocking=False)
    if lock_acquired:
        _cache_lock.release()
    return jsonify({
        "pid": os.getpid(),
        "lock_was_free": lock_acquired,
        "cache_keys": list(_cache.keys()),
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


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1")


threading.Thread(target=_prewarm, daemon=True).start()
