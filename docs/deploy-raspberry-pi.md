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

`<runner-user>` is whichever OS user the poll-and-deploy timer runs as
(normally just your own login user) -- bootstrap-pi.sh grants that user
passwordless sudo for exactly one command (`systemctl restart
thames-cso-dashboard.service`), nothing broader.

## Cloudflare quick tunnel (public HTTPS access, no domain/account needed)

Using a free quick tunnel for now: no Cloudflare account, no login, no
DNS -- `cloudflared` just gives you a random `https://<random>.trycloudflare.com`
URL. TLS is still handled entirely by Cloudflare's edge; gunicorn only
ever listens on `127.0.0.1:8000`, never exposed directly.

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb -o cloudflared.deb
# use cloudflared-linux-armhf.deb instead if `uname -m` says armv7l (32-bit,
# e.g. a Pi 3B+ on standard Raspberry Pi OS) -- NOT cloudflared-linux-arm.deb,
# that's a different, incompatible architecture tag despite the name
sudo dpkg -i cloudflared.deb

sudo cp deploy/cloudflared-quicktunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared-quicktunnel

# find the assigned URL (changes every time this service restarts):
sudo systemctl status cloudflared-quicktunnel --no-pager | grep trycloudflare
# or: grep trycloudflare /opt/thames-cso-dashboard/logs/cloudflared.log | tail -1
```

**Trade-off to know about:** the URL is only stable as long as the
service keeps running -- it changes on every restart (Pi reboot,
`cloudflared` crash-and-recover, etc). Since this is still a testing
phase with nobody else using the link yet, that's a fine trade for
avoiding any domain cost or Cloudflare account setup right now. Whenever
you're ready for a permanent link, moving to a real domain later is a
~15 minute change (new named tunnel + DNS record + swap this systemd
unit for one using `deploy/cloudflared-config.yml.example`) with zero
changes to the app itself -- see that file for the upgrade path.

If the URL changes, remember to update the `PI_DASHBOARD_URL` secret
(below) so the log-fetching workflow keeps working.

## Auto-deploy on every push (local poll-and-deploy timer)

`bootstrap-pi.sh` already installs and enables this -- nothing further to
do unless you're setting it up manually. It's a `thames-cso-poll-deploy`
systemd timer that runs `deploy/poll-and-deploy.sh` every 2 minutes: `git
fetch origin main`, and if there are new commits, `git reset --hard
origin/main` on the persistent checkout followed by
`deploy/sync-and-restart.sh` (rsyncs into `/opt/thames-cso-dashboard`,
reinstalls dependencies only if `requirements.txt` changed, restarts the
service).

Check it's running:

```bash
systemctl status thames-cso-poll-deploy.timer --no-pager
systemctl list-timers thames-cso-poll-deploy.timer --no-pager
```

**Why not a GitHub Actions self-hosted runner?** That was the original
plan -- instant deploys, ties into existing Actions usage -- but
registering one on this Pi (32-bit ARM, Raspberry Pi OS 13/Trixie) hit an
unresolved `.NET`/OpenSSL TLS handshake failure (`config.sh` couldn't
open an HTTPS connection to github.com at all, even though `curl` to the
same URL worked fine, pointing at a `.NET`-on-ARM32 certificate-store
issue rather than a real network problem). Rather than keep fighting an
unclear platform incompatibility, the local timer avoids the whole
category of problem -- it's plain `git` and `bash`, nothing .NET-based
running at all -- at the cost of a ~2 minute delay before a push takes
effect instead of near-instant.

Add these repository secrets (Settings -> Secrets and variables ->
Actions) so the log-fetching workflow below can reach the Pi:

- `PI_DASHBOARD_URL` -- e.g. `https://cso.yourdomain.com`
- `PI_LOGS_KEY` -- must match `LOGS_KEY` in the Pi's `.env`

## Checking logs without copying them by hand

`.github/workflows/pi-logs.yml` is a `workflow_dispatch`-triggered job
that runs on GitHub's own infrastructure (not the Pi), hits
`/ping`, `/data`, and `/internal/logs` on the public tunnel URL, and
prints the results into the workflow's own job log -- viewable directly
through the GitHub Actions UI or API, no copy-pasting required.

## Persistent logging across reboots

Off by default on Raspberry Pi OS (journald and the kernel's `dmesg`
buffer both live in RAM and are wiped on every reboot) -- worth doing
once, since it's the difference between having real evidence after a
crash and finding nothing at all:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/persistent-storage.conf > /dev/null << 'EOF'
[Journal]
Storage=persistent
SystemMaxUse=100M
EOF
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo systemctl restart systemd-journald
```

After a future reboot, `sudo journalctl -b -1 --no-pager | tail -150` and
`sudo journalctl -b -1 -k --no-pager | grep -i -E "oom|out of memory|killed process"`
will actually show what happened, instead of "no persistent journal was
found."

## Resource notes (869MB RAM measured, shared with the kiosk)

- `gunicorn.conf.py` already pins `workers = 1` -- keep it that way.
- Check `free -h` after everything is running; if it's tight, the kiosk's
  Chromium is almost certainly the larger consumer, not this Flask app.
- The poll-and-deploy timer is a `Type=oneshot` service that runs for a
  few seconds every 2 minutes and exits -- effectively zero idle
  footprint, unlike a persistent runner agent process would have been.
- Persistent journald logging is enabled (capped at `SystemMaxUse=100M`
  via `/etc/systemd/journald.conf.d/persistent-storage.conf`) so a future
  crash/reboot leaves `journalctl -b -1` evidence to look at, instead of
  the "no persistent journal was found" dead end this project hit once
  already.
