# Ghost Bus Tracker — Ops Runbook

One always-free Oracle Cloud VM, three systemd-managed Python services, SQLite,
static publishing. This document is the complete operational reference for
that VM. Every command below is meant to be copy-pasted as-is — the only
value you must supply yourself is the NTA API key.

---

## 1. Provisioning

Two tasks in this section can only be done by the account owner (Alex) —
they require personal accounts and cannot be scripted or delegated:

1. **NTA developer account + API key** — register for free at
   `https://developer.nationaltransport.ie`, subscribe to the GTFS-Realtime
   product (TripUpdates + VehiclePositions), and copy the API key it issues.
   The key is used only in step 3.3 below — it never enters the repo.
2. **Oracle Cloud account + free-tier VM** — sign up at
   `https://www.oracle.com/cloud/free/` (a credit card is required for
   identity verification, but the "Always Free" shapes below incur no
   charge).

### 1.1 Create the VM

In the OCI Console, create a compute instance with:

- **Shape:** `VM.Standard.E2.1.Micro` (AMD, 1/8 OCPU, 1 GB RAM) or
  `VM.Standard.A1.Flex` (Arm, sized to 1 OCPU / 6 GB RAM) — both are
  Always Free eligible. Prefer A1.Flex if your tenancy has Arm capacity
  available; it has meaningfully more headroom for the same price (free).
- **Image:** Canonical Ubuntu 24.04 (Minimal or Standard — either works).
- **Networking:** default VCN is fine. In the subnet's security list /
  the instance's Network Security Group, **do not open any inbound port
  beyond SSH (TCP/22)**. The poller, classifier, and publisher have no
  inbound network surface — they only make outbound HTTPS calls (to the
  NTA API, healthchecks.io, and GitHub). If the static site is ever served
  directly from this VM instead of GitHub Pages, open TCP/443 at that time,
  not before.
- **SSH keys:** upload your own public key at creation time; do not use
  password auth.
- Boot volume: default (46–50 GB) is more than enough.

### 1.2 Baseline hardening

SSH in as `ubuntu` and run:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3.12 python3.12-venv git sqlite3 ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw enable
sudo reboot
```

---

## 2. Install

SSH back in once the reboot completes, then:

```bash
sudo mkdir -p /opt/ghost-bus
sudo chown ubuntu:ubuntu /opt/ghost-bus
git clone https://github.com/aleks-drozy/ghost-bus.git /opt/ghost-bus
cd /opt/ghost-bus
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
mkdir -p state data
```

### 2.1 Environment file (the one place the API key lives)

```bash
sudo tee /etc/ghostbus.env > /dev/null <<'EOF'
NTA_API_KEY=<your key>
EOF
sudo chmod 600 /etc/ghostbus.env
sudo chown root:root /etc/ghostbus.env
```

Replace `<your key>` with the literal key from step 1, task 1. This file is
never committed, never logged, and is the only copy of the secret outside
the NTA developer portal itself.

### 2.2 Install the systemd units

```bash
sudo cp /opt/ghost-bus/ops/ghostbus-poller.service /etc/systemd/system/
sudo cp /opt/ghost-bus/ops/ghostbus-classifier.service /etc/systemd/system/
sudo cp /opt/ghost-bus/ops/ghostbus-classifier.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

### 2.3 Enable and start

```bash
sudo systemctl enable --now ghostbus-poller.service
sudo systemctl enable --now ghostbus-classifier.timer
```

The classifier's `.service` unit is `Type=oneshot` and is never started
directly by `enable --now` beyond the initial `enable`'s no-op — the timer
is what actually invokes it every 10 minutes going forward. Confirm both are
live:

```bash
systemctl status ghostbus-poller.service --no-pager
systemctl list-timers ghostbus-classifier.timer --no-pager
```

---

## 3. Health

### 3.1 Heartbeat one-liner

Confirms the poller wrote a successful heartbeat in the last 5 minutes:

```bash
sqlite3 /opt/ghost-bus/state/ghostbus.db \
  "SELECT ts_utc, ok FROM heartbeats ORDER BY ts_utc DESC LIMIT 5;"
```

Expect five rows, each `ok=1`, with `ts_utc` timestamps roughly 60 seconds
apart and the newest one within the last couple of minutes of `date -u`.

### 3.2 Logs

```bash
journalctl -u ghostbus-poller.service -f            # live tail
journalctl -u ghostbus-poller.service --since "1 hour ago"
journalctl -u ghostbus-classifier.service -n 50 --no-pager
```

### 3.3 healthchecks.io ping wiring

1. Create a free account at `https://healthchecks.io` and add a new check
   named `ghost-bus-poller` with a **period of 5 minutes** and a **grace
   time of 5 minutes** (so one missed poll doesn't page, two in a row
   does). Configure email (and Telegram, if wired) as the notification
   channel.
2. Copy the check's ping URL (`https://hc-ping.com/<uuid>`) into the
   env file alongside the API key:

   ```bash
   sudo sh -c 'echo "HEALTHCHECK_URL=https://hc-ping.com/<uuid>" >> /etc/ghostbus.env'
   ```

3. Add a ping to the end of every successful poll loop iteration in
   `ingest/run_poller.py` (the Phase-2 production entry point):

   ```python
   import os
   import requests
   hc_url = os.environ.get("HEALTHCHECK_URL")
   if hc_url:
       requests.get(hc_url, timeout=5)
   ```

4. Restart the poller after adding the entry point so it picks up the new
   env var: `sudo systemctl restart ghostbus-poller.service`.

Silence beyond the grace period triggers the configured alert — that alert
is the trigger for the Recovery steps below.

---

## 4. Recovery

### 4.1 Poller down / stuck

```bash
sudo systemctl status ghostbus-poller.service --no-pager
sudo systemctl restart ghostbus-poller.service
journalctl -u ghostbus-poller.service -n 100 --no-pager   # confirm it's polling again
```

If it immediately crash-loops, check `EnvironmentFile=/etc/ghostbus.env`
exists and is readable, and that `NTA_API_KEY` is set and not expired/revoked
in the NTA developer portal.

### 4.2 Timetable (GTFS static) refresh

The static timetable is normally refreshed automatically on a weekly
schedule, but force a refresh immediately if trip-match failures spike
(visible as a jump in EXCLUDED-adjacent classification errors, or in
`journalctl -u ghostbus-classifier.service`):

```bash
cd /opt/ghost-bus
.venv/bin/python -m timetable.refresh   # Phase-2 production entry point;
                                        # downloads the latest operator GTFS
                                        # zip from data.gov.ie, loads it via
                                        # timetable.gtfs.load_gtfs, and
                                        # records the new hash + validity
                                        # window in gtfs_meta.
sqlite3 /opt/ghost-bus/state/ghostbus.db \
  "SELECT value FROM gtfs_meta WHERE key='gtfs_hash';"   # confirm the hash changed
```

### 4.3 Disk cleanup — archives older than 7 days

Raw zstd snapshots are kept for 7 days by design (see `ingest/poller.py`).
If disk pressure appears sooner (`df -h /opt/ghost-bus`), clean up manually:

```bash
find /opt/ghost-bus/data -name '*.pb.zst' -mtime +7 -print -delete
```

Drop `-delete` first to dry-run and confirm the file list before deleting.
