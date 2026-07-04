# Thames Water CSO Dashboard

A national dashboard over every Combined Sewer Overflow (CSO) / Event Duration
Monitor (EDM) that Thames Water's Open Data API reports — live discharge
status, receiving watercourse, and discharge duration. This is a sibling
project to [frbc-tides](https://github.com/andrewfdn1/frbc-tides), which
tracks a hand-curated ~70 monitors near Fulham Reach Boat Club for rowing
safety; this one deliberately covers **every** permit the API knows about,
nationally, not just the Thames itself.

## Why this isn't just frbc-tides with a bigger config file

frbc-tides' `cso_monitors.json` is a hand-researched registry: each of its
~70 monitors has a manually-verified tunnel-connection status, sourced from
reading Thames Water's prose FAQ page permit-by-permit. That doesn't scale
to ~500+ monitors nationally, so this app takes a different, automated
approach:

- **No hand-curated monitor list.** `discharge/status` is pulled in full,
  paginated, on every cache refresh — whatever permits the API reports
  *are* the tracked set. Adding/retiring a permit on Thames Water's side
  needs no code or config change here.
- **Tunnel-connection is inferred, not manually verified.** The live API's
  own `receivingWaterCourse` field states `"River Thames (via the Tideway
  tunnel)"` for tunnel-connected permits vs plain `"River Thames"` otherwise
  — a structured, scalable signal. This is marked `tunnel_connected_inferred`
  throughout (map popups, monitor table) precisely because — unlike
  frbc-tides — it has not been cross-checked against Thames Water's
  site-by-site FAQ text.
- **Grouping is by receiving watercourse, not hand-placed geographic
  zones.** frbc-tides manually judged which of five zones each monitor
  belongs to. That doesn't scale either, so this app groups monitors by
  their reported watercourse (River Thames, River Wandle, River Brent, ...)
  instead.

## Data sources

| API | Purpose | Key required |
|-----|---------|--------------|
| Thames Water Open Data API v2 — `discharge/status` | Full national list of every monitored permit: name, coordinates, receiving watercourse, live alert status | No |
| Thames Water Open Data API v2 — `discharge/alerts` | Historic Start/Stop events, used to compute discharge duration per window | No |

**No API keys or secrets are required at all** — both endpoints are open
data. This is a meaningful deployment simplification versus frbc-tides
(which depends on five separately-provisioned API keys): there's nothing to
configure in Render's environment variables beyond the service itself.

## Why the lookback window is 24h/7d, not 30d

frbc-tides fetches `discharge/alerts` unfiltered by permit already — it
pages through every national Start/Stop event and filters to its ~70
tracked permits in Python. That means expanding *coverage* to all permits
doesn't add API calls on its own. What does add load is this app's much
larger *result set*: a national Start+Stop sweep over 30 days would be a
lot more paginated requests (at 1 req/sec, self-throttled) than the same
sweep over 7 days. To keep a cold-cache fetch bounded to roughly a minute
rather than several, this app only computes 24-hour and 7-day discharge
duration windows. A 30-day window could be added later if it proves
useful, at the cost of a slower refresh.

## Caching

Both endpoints are pulled by a background thread at process startup
(`_prewarm`, matching the pattern already proven in frbc-tides) and then
re-fetched on demand once the 30-minute cache TTL expires. Because Render's
free tier fully suspends the process on spin-down, there is no always-on
scheduler here — a fresh request after spin-down triggers the same
startup-time prewarm behaviour as a cold deploy.

## Deployment

This app is deployed to a **separate Render account** from frbc-tides,
not the same workspace. Render's free tier grants 750 instance-hours/month
*per workspace*, shared across every free web service in that workspace —
so a second free service under the same account/workspace as frbc-tides
would draw down the same pool. A separate Render account gets its own
independent 750-hour pool.

To deploy:

1. Push this repository to GitHub (already done if you're reading this
   from the repo).
2. Sign in to a Render account that is **not** the one hosting frbc-tides.
3. **New → Web Service**, select this repository.
4. No environment variables are required.
5. Render will detect `render.yaml` and configure the build/start commands
   automatically.

## API Endpoints

- `GET /` — summary dashboard (national totals, top waterways by active discharge)
- `GET /map` — Leaflet map of every tracked monitor, colour-coded by live status
- `GET /monitors` — full searchable/filterable table of every tracked monitor
- `GET /data` — JSON dump of the full dataset
- `GET /ping` — health check

## Local development

```bash
pip install -r requirements.txt
python app.py
# Visit http://localhost:5000
```

## File structure

```
thames-water-cso-dashboard/
├── app.py                # Flask app + Thames Water API fetch/cache logic
├── requirements.txt
├── render.yaml            # Render.com deployment config
├── gunicorn.conf.py        # 180s timeout — a cold-cache national fetch takes longer than frbc-tides' curated pull
├── templates/
│   ├── _nav.html
│   ├── index.html
│   ├── map.html
│   └── monitors.html
└── static/
    └── style.css
```
