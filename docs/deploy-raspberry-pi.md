# Deploying on the Raspberry Pi

Runs alongside the Pi's primary job (headless Chromium kiosk showing
frbc-tides). Data stays in the same two Turso databases used previously;
only the hosting platform changes. See `db.py`'s module docstring for why
Turso is accessed over plain HTTP rather than the native `libsql` client
-- that reasoning applies here too, not just on Render.

## One-time setup on the Pi

```bash
git clone https://github.com/andrewfdn1/thames-water-cso-dashboard.git
cd thames-water-cso-dashboard
cp deploy/env.example .env   # then fill in real Turso creds + generate
                              # DISCHARGE_SYNC_KEY / LOGS_KEY with:
                              #   openssl rand -hex 24
mv .env /tmp/thames-cso.env  # bootstrap-pi.sh copies the repo into /opt,
                              # not this checkout -- move .env there after
bash deploy/bootstrap-pi.sh <runner-user>
sudo cp /tmp/thames-cso.env /opt/thames-cso-dashboard/.env
sudo systemctl start thames-cso-dashboard
sudo systemctl status thames-cso-dashboard   # should be active (running)
curl -s http://127.0.0.1:8000/ping           # should return {"status": "ok"} or similar
```

`<runner-user>` is whichever OS user will run the GitHub Actions
self-hosted runner (see below) -- bootstrap-pi.sh grants that user
passwordless sudo for exactly one command (`systemctl restart
thames-cso-dashboard.service`), nothing broader.

## Cloudflare Tunnel (public HTTPS access)

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb -o cloudflared.deb
# use cloudflared-linux-arm.deb instead if `uname -m` says armv7l/armhf, not aarch64
sudo dpkg -i cloudflared.deb

cloudflared tunnel login                              # opens a browser link, pick your domain's zone
cloudflared tunnel create thames-cso-dashboard
cloudflared tunnel route dns thames-cso-dashboard cso.yourdomain.com

sudo mkdir -p /etc/cloudflared
cp deploy/cloudflared-config.yml.example /etc/cloudflared/config.yml
# edit /etc/cloudflared/config.yml: fill in the real tunnel ID (from the
# `tunnel create` output) and your real hostname

sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

The dashboard is now live at `https://cso.yourdomain.com`, TLS handled
entirely by Cloudflare's edge -- gunicorn only ever listens on
`127.0.0.1:8000`, never exposed directly.

## Auto-deploy on every push (GitHub Actions self-hosted runner)

On the Pi, following GitHub's own instructions (Settings -> Actions ->
Runners -> New self-hosted runner on the repo), then:

```bash
sudo ./svc.sh install <runner-user>
sudo ./svc.sh start
```

so the runner survives reboots as its own systemd service, same as the
app itself.

Add these repository secrets (Settings -> Secrets and variables ->
Actions) so the log-fetching workflow below can reach the Pi:

- `PI_DASHBOARD_URL` -- e.g. `https://cso.yourdomain.com`
- `PI_LOGS_KEY` -- must match `LOGS_KEY` in the Pi's `.env`

`.github/workflows/deploy-to-pi.yml` then runs on the Pi's own runner on
every push to `main`: checks out the new code, runs
`deploy/sync-and-restart.sh` (rsyncs into `/opt/thames-cso-dashboard`,
reinstalls dependencies only if `requirements.txt` changed, restarts the
service, and fails the workflow if it doesn't come back up healthy).

## Checking logs without copying them by hand

`.github/workflows/pi-logs.yml` is a `workflow_dispatch`-triggered job
that runs on GitHub's own infrastructure (not the Pi), hits
`/ping`, `/data`, and `/internal/logs` on the public tunnel URL, and
prints the results into the workflow's own job log -- viewable directly
through the GitHub Actions UI or API, no copy-pasting required.

## Resource notes (1GB RAM, shared with the kiosk)

- `gunicorn.conf.py` already pins `workers = 1` -- keep it that way.
- Check `free -h` after everything is running; if it's tight, the kiosk's
  Chromium is almost certainly the larger consumer, not this Flask app.
- The GitHub Actions runner's own background agent has a real, constant
  RAM footprint (Node-based) -- if memory pressure becomes a problem,
  switching `deploy-to-pi.yml`'s trigger to a lightweight polling script
  (`git fetch` + compare SHA on a systemd timer) removes that footprint
  entirely without changing anything else.
