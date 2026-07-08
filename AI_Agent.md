# AI Agent Notes

Technical reference for picking up this project cold — a new Claude Code
session, another AI, or a human unfamiliar with the codebase. Written to
be self-contained: everything here either isn't obvious from reading the
code alone, or is a fact (API limits, hardware specs, external service
config) that isn't in the code at all.

## What this app is

A Flask dashboard tracking every Combined Sewer Overflow (CSO) / Event
Duration Monitor (EDM) permit Thames Water's Open Data API reports
nationally (~500+ monitors), plus E. coli water-quality readings from two
testing sites (FRBC, PTRC). Sibling project to `frbc-tides` (a smaller,
hand-curated ~70-monitor dashboard) — this one deliberately covers
everything the API knows about, not a curated subset. See `README.md` for
the feature-level pitch; this file is about implementation traps, not
features.

## Architecture at a glance

- `app.py` — the whole Flask app: routes, live API fetching/caching,
  water-quality persistence, discharge-history reads, the two `/internal/*`
  operational endpoints. Single file by design so far; ~1400 lines.
- `db.py` — shared DB connection helper. Read this before touching any
  storage code; see "Turso" section below for why it's not a normal
  sqlite3-alike.
- `scripts/backfill_discharge_history.py` — standalone, resumable
  historical backfill/repair tool. Imported directly by `app.py`
  (`import backfill_discharge_history as _discharge_backfill`) so the
  live app's auto-sync reuses its fetch/pair/upsert logic rather than
  duplicating it.
- `scripts/diagnose_watercourse.py` — read-only diagnostic tool for
  investigating data-quality issues in `discharge_events` (flatlines,
  mispaired Start/Stop, suspiciously repeating durations). Has a live-API
  verification mode (`--verify`) that cross-checks stored data against a
  fresh pull from Thames Water directly.
- `templates/` — Jinja2, one template per page, no JS framework (vanilla
  JS + Chart.js 4.4.0 via CDN for the Testing page charts, Leaflet for the
  map).
- `deploy/` — Raspberry Pi deployment artifacts (systemd units, cloudflared
  config, bootstrap/sync scripts). See "Deployment" below.
- `.github/workflows/` — CI/diagnostic/deploy workflows, several designed
  to be triggered on demand (`workflow_dispatch`) specifically so an AI
  agent without shell access to the target machine can still get real
  operational data back via the GitHub Actions job log.

## External APIs

### Thames Water Open Data API v2 (no key required)

- `discharge/status` — full national list of every monitored permit
  (name, coordinates as British National Grid easting/northing converted
  to WGS84 in `app.py`'s `_bng_to_wgs84`, receiving watercourse, live
  `alertStatus`). This is the source of truth for which permits exist —
  there is no hand-curated monitor list anywhere in this codebase.
- `discharge/alerts` — historic Start/Stop events, `alertType=Start` or
  `alertType=Stop`, filtered by `dateStart`/`dateEnd` (YYYY-MM-DD).
- Pagination: 200 items/page (`limit`/`offset` params) — **larger page
  sizes have been observed to return HTTP 500**, don't increase this.
- Self-throttled to ~1 request/second (`time.sleep(1)` between pages) with
  exponential backoff retry on HTTP 429. Documented (not currently
  enforced) rate limit is 5 requests/second/user — everything in this repo
  stays well under that deliberately.
- Update cadence: new data appears roughly every 30 minutes, with a
  further processing delay meaning a discharge is normally visible within
  about an hour of actually starting. This is *why* the auto-sync interval
  is 30 minutes (`_DISCHARGE_SYNC_INTERVAL_SECONDS` in `app.py`) — polling
  faster than the source refreshes gains nothing.
- Historical data is documented back to April 2022, but the API's own docs
  warn visibility before the official "go live" date is limited/non-
  representative. `GO_LIVE_DATE = date(2022, 12, 30)` in
  `backfill_discharge_history.py` is the deliberate starting point for
  that reason — don't backfill earlier than this.
- **Known failure mode, already handled**: this API can return a valid
  HTTP 200 with zero items under sustained load — indistinguishable from
  a genuinely quiet period at a glance. `fetch_chunk()` in the backfill
  script retries a whole chunk up to 3x unless *both* Start and Stop come
  back non-empty. `get_discharge_windows()` in `app.py` treats an
  all-empty national 30-day pull as a bad response (raises, so the cache
  serves last-known-good data) rather than caching "nothing is discharging
  nationally," which has never once been a genuine result.

### Google Sheets (E. coli readings)

- Two sheets, FRBC and PTRC, fetched as CSV via the public
  `/export?format=csv&gid=...` URL pattern — no API key, no auth, just a
  GET. URLs are hardcoded in `app.py` (`_WQ_FRBC_URL`, `_WQ_PTRC_URL`).
- Sheets are hand-updated by their owners roughly weekly. Fetch interval
  is deliberately daily (`_WQ_REFRESH_SECONDS = 86400`), not more
  frequent — there's nothing to gain from polling faster than a human
  updates the source, and it keeps this to one small CSV fetch/day/site
  once running continuously on the Pi.
- Column names are NOT consistent between the two sheets ("Reading
  E.Coli/100ml" vs "Alert One E.Coli reading (CFU per 100ml)") and could
  change again if a third source is added — `_wq_find_ecoli_col` matches
  by keyword, not exact header text, and logs a warning if it finds zero
  or multiple candidates rather than silently guessing wrong.
- A sheet returning HTTP 403 is treated as "genuinely not public," not a
  transient error (no retry) — distinct from other HTTP errors, which do
  propagate as failures.

## Data storage: Turso, over plain HTTP — read this before touching `db.py`

Two separate Turso (hosted libSQL/SQLite-compatible) databases:

- `thames-cso-water-quality` — E. coli readings (`ecoli_readings` table).
  Env prefix `TURSO` (i.e. `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN`).
- `thames-cso-discharge-history` — discharge Start/Stop event history
  (`discharge_events` table, plus `backfill_progress` and
  `discharge_sync_progress` checkpoint tables). Env prefix
  `TURSO_DISCHARGE` (i.e. `TURSO_DISCHARGE_DATABASE_URL` /
  `TURSO_DISCHARGE_AUTH_TOKEN`).

**Critical, non-obvious fact: `db.py` does NOT use the native `libsql`
Python client.** It speaks Turso's HTTP API directly (`Hrana-over-HTTP`,
the `/v2/pipeline` endpoint) via plain `requests`. This was a deliberate
rewrite, not a style choice — do not "simplify" this back to
`import libsql; libsql.connect(...)` without reading the reasoning below
and checking whether the underlying upstream bug has actually been fixed.

**Why**: the native `libsql` client wraps a Rust/Tokio async runtime.
In production, the moment it was used from a long-running Flask/gunicorn
process (not a short-lived script), it deadlocked the entire worker — the
GIL never released, gunicorn's own worker-timeout eventually SIGKILLed the
stuck worker, a fresh worker booted, and the identical deadlock recurred
seconds later. Endless boot-crash loop. This reproduced identically
whether the connection was made from a background `threading.Thread` or
from a normal Flask request-handling thread — it is not thread-specific,
it is specific to *long-running process + native libsql client*, which is
why a short manual script (`backfill_discharge_history.py` run from a
terminal) never triggers it: the process exits before the buggy teardown
path is ever hit. This is a documented, unresolved, upstream issue across
multiple Turso/libsql Python packages, not something specific to Render or
any one host:
- `tursodatabase/libsql` issue #1075 ("no reactor running" panic during
  object destruction/GC).
- `tursodatabase/libsql-client-py` issue #30 — the exact same
  Flask-triggers-a-deadlock symptom against a local db; that repo is now
  **archived** (June 2025) with no fix ever posted.
- Turso's own org has since started a from-scratch rewrite
  (`tursodatabase/turso`), separate from the `libsql` bindings — a signal
  even they're moving away from this architecture.

**`TursoHttpConnection` (in `db.py`)** mimics only the sqlite3 surface
this codebase actually uses (confirmed by grep across the whole repo, so
check again if new query patterns are added): `.execute(sql, params)`,
`.fetchall()`/`.fetchone()` on the result, `with conn:` as a context
manager, `.close()`. No `executemany`, no `.lastrowid`/`.rowcount`, no row
dict access. Rows come back as plain tuples (sqlite3's default
row_factory). `with conn:` batches every queued statement into one HTTP
`/v2/pipeline` call on clean exit (one round-trip for a whole write loop)
and discards the batch entirely if an exception occurs inside the block —
it does **not** send a partial batch.

`db.connect(local_path, env_prefix="TURSO")` falls back to a plain local
`sqlite3.connect(local_path)` file whenever that prefix's two env vars
aren't both set — this is how local dev / testing avoids needing any
Turso credentials at all.

**Testing methodology**: since Thames Water's API and Turso are both
unreachable from some sandboxed dev environments, tests mock at the
`requests.Session.post` layer with a small in-memory-SQLite-backed fake
Turso server (encodes/decodes the real Hrana JSON value types) rather than
mocking `db.py` itself — this exercises the actual encoding logic, not
just the call sites. Search git history / PR descriptions for
"FakeTursoServer" if you need to write a similar test. Always set
`os.environ["DISCHARGE_AUTO_SYNC"] = "0"` before importing `app` in any
test or one-off script — see next section for why.

## Background threads vs. HTTP-triggered work — don't reintroduce a mixed pattern

- `_prewarm()` (fetches `get_all_monitors()` + `get_discharge_windows()`
  from Thames Water, no database involved) **is** a background
  `threading.Thread`, started unconditionally at true module level (not
  inside `if __name__ == "__main__":`) so it runs correctly whether
  launched via `python app.py` or `gunicorn app:app`. This is safe because
  it never touches Turso/libsql.
- The discharge-history auto-sync (`_sync_discharge_history_once`) is
  **deliberately not** a background thread — it's only reachable via
  `GET /internal/sync-discharge-history`, meant to be hit periodically by
  an external scheduler (currently: a `cron`/systemd-timer style poke; see
  Deployment section). This is a direct consequence of the Turso deadlock
  above — moving it off a background thread was the first (partial) fix
  attempt before the deeper fix (dropping the native client entirely).
  Both changes are needed; don't revert either independently.
- `DISCHARGE_AUTO_SYNC` env var (default `"1"`/enabled) gates whether the
  sync endpoint does anything or returns `503 disabled` — it does **not**
  gate a thread anymore, despite the name's history.
- `gunicorn.conf.py` pins `workers = 1` — auto-loaded by gunicorn without
  a `-c` flag since it's named `gunicorn.conf.py` in the working
  directory (gunicorn's documented default config lookup). Keep this at 1:
  the prewarm thread would otherwise start once per worker process, and
  on the Pi's 1GB RAM (shared with a kiosk Chromium instance) extra
  workers aren't affordable anyway.

## Two secret-gated `/internal/*` endpoints

- `GET /internal/sync-discharge-history?key=...` — runs one discharge-
  history sync cycle (catch-up + bounded unclosed-record retries).
  Requires `DISCHARGE_SYNC_KEY` env var to be set **and** match — no
  "open if unset" fallback (there used to be one; removed once this became
  reachable on a stable public domain instead of Render's obscure default
  subdomain).
- `GET /internal/logs?key=...&lines=N` — plain-text tail of the app's own
  log file (default 200 lines, max 2000). Requires `LOGS_KEY` env var,
  same no-fallback rule. Reads from `APP_LOG_PATH` (env var, default
  `/opt/thames-cso-dashboard/logs/app.log`) — on the Pi this file is
  populated by systemd's `StandardOutput=append:...`/`StandardError=append:...`
  directives in `deploy/thames-cso-dashboard.service`, capturing this
  app's plain `print()` statements (there's no structured logging
  framework — `print()` to stdout is the entire logging strategy, by
  design, matching how Render's log viewer worked previously).
- `.github/workflows/pi-logs.yml` is a `workflow_dispatch`-triggered
  workflow that runs on GitHub's own infrastructure (not the Pi) and
  curls `/ping`, `/data`, and `/internal/logs` on the Pi's public tunnel
  URL, printing results into the job's own log — the intended way for an
  AI agent (or anyone) without shell access to the Pi to see what's
  happening, without a human copy-pasting logs into a chat. Needs repo
  secrets `PI_DASHBOARD_URL` and `PI_LOGS_KEY` set.

## Deployment: Raspberry Pi (current), Render (dormant, kept for reference)

**Currently deployed on a Raspberry Pi 3B+ (1GB RAM), Raspberry Pi OS 13
(Trixie)**, whose primary job is running headless Chromium as a kiosk
displaying `https://frbc-tides.onrender.com/` on an attached HDMI display.
This app runs alongside that using spare capacity — it is not the
device's main purpose, so resource usage (RAM especially) matters more
here than it would on a dedicated host.

Full step-by-step runbook: `docs/deploy-raspberry-pi.md`. Key facts not
obvious from the code:

- Public HTTPS via a **named Cloudflare Tunnel**, routed to a subdomain
  (tested against `waterquality.edwarddoughty.com`, a spare unused domain
  on the same WordPress.com account, before committing to buying a
  dedicated domain — `waterquality.uk` was the intended permanent home at
  time of writing; check `deploy/cloudflared-config.yml.example` and
  `/etc/cloudflared/config.yml` on the Pi for whatever the current live
  hostname actually is). Started out on a **quick tunnel**
  (`cloudflared tunnel --url ...`, no account needed, but the URL changes
  on every restart) before upgrading — `deploy/cloudflared-quicktunnel.service`
  is kept in the repo as that fallback/starting option, but isn't what's
  actually running once a named tunnel is configured.
  **Important, non-obvious fact discovered the hard way**: Cloudflare's
  self-service "Add a Site" onboarding only accepts root/registrable
  domains, not subdomains — you cannot add `foo.example.com` as its own
  zone through the normal UI. Routing a subdomain therefore requires
  onboarding the *entire* root domain to Cloudflare (nameservers moved
  there), which is why a spare, unused domain was used for testing rather
  than a subdomain of a domain with an active site/email on it.
- gunicorn binds `127.0.0.1:8000` only, never `0.0.0.0` — the tunnel is
  the only path in from the public internet.
- Auto-deploy is a **local systemd timer** (`thames-cso-poll-deploy.timer`,
  installed by `bootstrap-pi.sh`), not a GitHub Actions self-hosted
  runner — a runner was the original plan but registering one
  (`config.sh`) hit an unresolved `.NET`/OpenSSL TLS handshake failure
  specific to 32-bit ARM (`armv7l`) + Raspberry Pi OS 13/Trixie (`curl`
  to the same URL worked fine, ruling out a real network problem — this
  points at a `.NET`-on-ARM32 certificate-store issue, not something this
  app's code can fix). The timer runs `deploy/poll-and-deploy.sh` every 2
  minutes: `git fetch origin main`, and if there are new commits,
  `git reset --hard origin/main` on the persistent checkout followed by
  `deploy/sync-and-restart.sh <checkout-dir>` (rsyncs into
  `/opt/thames-cso-dashboard`, excluding `.git`, `venv`, `logs`, `.env`;
  reinstalls Python deps only if `requirements.txt` changed; restarts the
  systemd service). That user has narrowly-scoped passwordless sudo for
  exactly one command (`systemctl restart thames-cso-dashboard.service`),
  set up by `deploy/bootstrap-pi.sh` — nothing broader. Trade-off versus a
  runner: a ~2 minute delay before a push takes effect, versus near-instant
  — accepted deliberately to avoid an unresolved platform incompatibility
  and an extra persistent agent process on an already memory-constrained
  Pi.
- Persistent journald logging (`/etc/systemd/journald.conf.d/persistent-storage.conf`,
  capped at `SystemMaxUse=100M`) was enabled after a Pi hang left zero
  diagnostic evidence (`journalctl -b -1` said "no persistent journal was
  found" — Raspberry Pi OS defaults to volatile, RAM-only journal storage
  that's wiped on every reboot). Without this, any future crash is
  undiagnosable after the fact.
- `render.yaml` and the Render-specific README section are kept but
  dormant — this app is not currently running on Render. If reviving
  Render deployment, re-read the Turso/libsql section above first; nothing
  about "which host" changes that reasoning, since the failure mode is
  about long-running-process + native client, not about Render
  specifically.

## Environment variables (complete list)

| Variable | Required? | Purpose |
|---|---|---|
| `TURSO_DATABASE_URL` | For Turso-backed water quality | `libsql://...turso.io` URL |
| `TURSO_AUTH_TOKEN` | For Turso-backed water quality | from `turso db tokens create thames-cso-water-quality` |
| `TURSO_DISCHARGE_DATABASE_URL` | For Turso-backed discharge history | `libsql://...turso.io` URL |
| `TURSO_DISCHARGE_AUTH_TOKEN` | For Turso-backed discharge history | from `turso db tokens create thames-cso-discharge-history` |
| `DISCHARGE_SYNC_KEY` | Required to use `/internal/sync-discharge-history` | shared secret, e.g. `openssl rand -hex 24` |
| `LOGS_KEY` | Required to use `/internal/logs` | shared secret, same generation method |
| `APP_LOG_PATH` | Optional | default `/opt/thames-cso-dashboard/logs/app.log` |
| `DISCHARGE_AUTO_SYNC` | Optional | default `"1"` (enabled); `"0"` disables the sync endpoint (returns 503) — **always set to `"0"` before importing `app` in tests/scripts** to avoid the module doing unwanted work on import |
| `FLASK_DEBUG` | Optional | default `"0"`; never set to `"1"` in production |

Any store missing its pair of `_DATABASE_URL`/`_AUTH_TOKEN` vars falls
back silently to a local SQLite file under `data/` — useful for local dev,
but means a typo'd/missing env var on a real deployment fails silently
into non-persistent local storage rather than an obvious error. If data
mysteriously isn't persisting across restarts, check this first.

## Key non-obvious behaviors / gotchas when modifying this code

- **`_DISCHARGE_OPEN_MAX_ASSUMED_HOURS = 48`**: a discharge event with no
  Stop event yet is only ever assumed to run 48 hours past its Start, not
  indefinitely to "now." Removing this cap reintroduces a real historical
  bug: a stuck-open record from months/years ago would silently inflate
  every week-bucket in between with a flat-lined, wildly wrong total
  (this exact bug previously made one watercourse show a flat 336 hrs/week
  — 2x168 — for over two years).
- **Zone/watercourse canonicalization** (`canonical_zone` inside
  `get_total_discharge_weekly`, and `_normalise_watercourse` at module
  level) unifies name variants (anything containing "thames" → "River
  Thames"; strips the "(via the Tideway tunnel)" suffix). There is
  deliberately **no cap** on how many zones appear in the Testing page
  dropdown — a previous 15-zone "busiest only" cap silently hid legitimate
  tributaries (e.g. River Brent) from the UI; don't reintroduce a cap
  without surfacing every zone some other way.
- **`_week_buckets(range_start, range_end)`**: the final bucket's end is
  exclusive and offset one day past `range_end` — omitting that offset
  silently drops "today"'s own events from every bucket when `range_end`
  is today (a previously-fixed off-by-one).
- **Unclosed-discharge bounded retry** (`_DISCHARGE_UNCLOSED_MAX_RETRIES = 5`,
  `_DISCHARGE_UNCLOSED_BATCH_LIMIT = 20`, `_DISCHARGE_UNCLOSED_MIN_AGE_DAYS = 2`):
  a still-open record only gets re-checked against the live API if it's
  at least 2 days old (recent opens are plausibly still genuinely
  ongoing), capped at 20 re-check attempts per sync cycle, and gives up on
  any single record after 5 failed resolve attempts (tracked via a
  `retry_count` column) rather than retrying forever. These three numbers
  were chosen from rough real-world scale (typical concurrent discharges:
  1–6 nationally; observed backlog at design time: ~11), not rigorously
  derived — revisit if the backlog ever grows much larger.
- **`backfill_discharge_history.py` vs. the live app's auto-sync are
  deliberately separate checkpoints** (`backfill_progress` table vs.
  `discharge_sync_progress` table) so a fresh environment's auto-sync
  never accidentally triggers an hours-long full historical crawl — the
  auto-sync only ever does short recent catch-ups (a day or two), the
  manual backfill script is the only thing that does deep history.
- **`fetch_chunk`'s retry-unless-both-non-empty logic**
  (`backfill_discharge_history.py`) exists because this API has been
  observed returning a valid HTTP 200 with genuinely asymmetric or empty
  results under load (e.g. 1200 starts but 0 stops for the same window) —
  a single-shot fetch can't distinguish this from a real data gap. Don't
  "simplify" this to a single fetch without reintroducing that risk.
- **`process_chunk`'s pairing assumes at most one open discharge per
  permit at a time** — matches how the live app treats a permit's status
  as a single current state. `diagnose_watercourse.py`'s "OVERLAPS
  previous" check exists specifically to catch violations of this
  assumption (a real pairing bug, not a data gap, if it ever fires).
- **`diagnose_watercourse.py`'s repeating-exact-duration check is low-
  signal at national scale** — permits with hundreds of events show huge
  repeat counts purely because durations are quantized to 15-minute API
  timestamps (a limited discrete value space makes coincidental repeats
  common), *not* because of a bug. It's only meaningful for a single
  permit with few events, or the cross-permit variant in the same script
  (same exact duration recurring across *different* permits under one
  watercourse — much stronger signal of a systematic bug).
- **A single very long (weeks-long) discharge interval is not necessarily
  a bug.** Verified via `diagnose_watercourse.py --verify PERMIT START END`
  against the live API directly: some genuinely are single continuous
  Start→Stop pairs with nothing hiding in between, correlating with known
  extreme wet-weather periods (e.g. Storm Babet, Oct 2023). Always verify
  against the live API before assuming a long interval is a pairing bug —
  the `--verify` mode exists exactly for this.
- **British National Grid → WGS84 conversion** (`_bng_to_wgs84`) is a
  from-scratch OSGB36 implementation, accurate to a few metres (fine for
  a map pin, not survey-grade) — don't be surprised by the amount of math
  here, it's not overengineering, it's what's needed to place a marker
  from the API's native easting/northing.
- **Per-key cache locks** (`_get_lock` in `app.py`), not one lock shared
  across every cache key — a previous single shared lock caused a
  concurrent request for a *different*, not-yet-cached key to silently
  return `None` (no retry) whenever it lost the race against a slow
  prewarm fetch holding the one lock. `get_cached()` is also deliberately
  non-blocking: a cache miss while another thread is already fetching
  serves stale/absent data immediately rather than blocking the request
  thread on a live network call (this previously let gunicorn's own
  worker-timeout kill the process mid-request).
- **Unhandled exceptions are logged with a full traceback to stdout**
  (`_log_unhandled_exception`) rather than only returning a bare 500 —
  this is what makes tracebacks visible via `/internal/logs` /
  `journalctl` rather than being swallowed by Flask's default error
  handling.

## Testing this app without live network access

Every route can be exercised via Flask's test client with these mocked:
`get_all_monitors`, `get_water_quality`, `_discharge_backfill.fetch_all_pages`,
`_discharge_backfill.run_chunks`, and (for Turso-backed code paths)
`requests.Session.post` patched to a fake in-memory-SQLite-backed Turso
server as described above. Always set
`os.environ["DISCHARGE_AUTO_SYNC"] = "0"` **before** `import app`, or a
real sync attempt can run during the test. Routes to sanity-check after
any change: `/`, `/map`, `/monitors`, `/testing`, `/unclosed-discharges`,
`/data`, `/ping`, `/debug-cache`, plus both `/internal/*` endpoints'
auth paths (no key / wrong key / correct key).
