# Multi-user deployment (process-per-user isolation)

This dashboard runs as **one Flask process per user**. Each instance reads its
own `.env`, has its own data directory, and listens on its own port. Users
cannot reach each other's state, Kite clients, or trading data through any
in-process path — that's enforced by the Linux kernel, not by application code.

## Layout on the VM

```
/opt/kite/                    ← shared code (git checkout of omkarc19/algotrading)
  app.py, templates/, bin/, deploy/, requirements.txt
  venv/                       ← shared Python venv

/opt/kite-data-omkar/         ← primary instance's data (port 80 / Flask 5001)
  .env
  strategies.json
  data/csv/...

/opt/kite-data-priya/         ← second user's data (port 8001 / Flask 5002)
  .env
  strategies.json
  data/csv/...

/etc/systemd/system/
  kite-monitor.service         ← primary (legacy name; reads /opt/kite-data-omkar)
  kite-monitor-priya.service
  kite-monitor-<slug>.service

/etc/nginx/sites-enabled/
  kite                         ← primary
  kite-priya
  kite-<slug>

/etc/nginx/.htpasswd-omkar
/etc/nginx/.htpasswd-priya
```

## Ports

| User | Public port (nginx) | Flask port (localhost) |
|---|---|---|
| omkar (primary) | 80 | 5001 |
| 2nd user | 8001 | 5002 |
| 3rd user | 8002 | 5003 |
| Nth user | 8000 + N - 1 | 5001 + N - 1 |

`bin/add_user.sh` picks the next free pair automatically.

**Important:** every public port needs to be opened in the AWS security group.
The script prints a reminder; you do it via the AWS console.

## Daily code deploys

```bash
# Local
git add ...
git commit -m "..."
git push

# VM — pull once, restart all instances
ssh kite-vm 'cd /opt/kite && git pull && \
             sudo systemctl restart "kite-monitor*"'
```

All instances run the same code from `/opt/kite/`, so one `git pull` + a
glob-restart updates everyone at once.

## Adding a user

```bash
ssh kite-vm
sudo bash /opt/kite/bin/add_user.sh priya
# script prompts for an htpasswd password, prints next steps
sudo -u ubuntu nano /opt/kite-data-priya/.env   # fill in Kite + Telegram creds
# Open inbound TCP <PORT_PRINTED_BY_SCRIPT> in AWS Security Group
sudo systemctl enable --now kite-monitor-priya
sudo systemctl reload nginx
```

User accesses at `http://<vm-ip>:<port>/` with the basic-auth credentials you set.

## Removing a user

```bash
sudo bash /opt/kite/bin/remove_user.sh priya
```

This stops the service, removes systemd + nginx configs, **archives the user's
data** to `/opt/kite-archive-priya-<timestamp>.tar.gz` (chmod 600), then
deletes the live data dir. Don't forget to close their port in the AWS
security group.

## Migration: from single-user `/opt/kite/` to `/opt/kite-data-omkar/`

The primary instance ('omkar') is special: its public port stays at 80, its
data lives at `/opt/kite-data-omkar/`, and its systemd unit keeps the legacy
name `kite-monitor` (no slug suffix). The migration is a one-time move of
`.env`, `strategies.json`, and `data/csv/` into the new data dir, plus a
small systemd unit edit to set `DATA_DIR` and `WorkingDirectory`. Steps:

```bash
ssh kite-vm

sudo systemctl stop kite-monitor

# 1. Move per-user data out of /opt/kite into /opt/kite-data-omkar
sudo mkdir -p /opt/kite-data-omkar/data
sudo mv /opt/kite/.env             /opt/kite-data-omkar/.env
sudo mv /opt/kite/strategies.json  /opt/kite-data-omkar/strategies.json || true
sudo mv /opt/kite/data/csv         /opt/kite-data-omkar/data/csv
sudo chown -R ubuntu:ubuntu /opt/kite-data-omkar
sudo chmod 600 /opt/kite-data-omkar/.env

# 2. data/nse_holidays_2026.json stays in code repo (reference data)
ls /opt/kite/data/nse_holidays_2026.json    # should still exist

# 3. Update systemd unit to point at the new dir
sudo tee /etc/systemd/system/kite-monitor.service > /dev/null <<'EOF'
[Unit]
Description=Kite MTM Monitor Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/kite-data-omkar
EnvironmentFile=/opt/kite-data-omkar/.env
Environment="DATA_DIR=/opt/kite-data-omkar"
Environment="PORT=5001"
ExecStart=/opt/kite/venv/bin/python /opt/kite/app.py
Restart=on-failure
RestartSec=5
StandardOutput=append:/var/log/kite-monitor.log
StandardError=append:/var/log/kite-monitor.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl start kite-monitor
sudo systemctl status kite-monitor --no-pager | head -10
```

Once that's running cleanly with all your strategies/data intact, you can add
new users via `bin/add_user.sh`.

## Observability

```bash
# All instances
systemctl list-units 'kite-monitor*'

# One user's logs
journalctl -u kite-monitor-priya -f

# All users at once
sudo tail -f /var/log/kite-monitor*.log

# Memory per instance
systemctl status 'kite-monitor*' --no-pager | grep -E "kite-monitor|Memory"
```

Resource caps are set per-instance: `MemoryMax=512M` and `CPUQuota=80%` so one
user's runaway process can't starve others. Tune in
`deploy/templates/kite-monitor-user.service.tmpl` if needed.
