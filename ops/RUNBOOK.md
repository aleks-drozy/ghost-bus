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

`ghostbus-poller.service` and `ghostbus-classifier.service` `ExecStart` into
`ingest.run_poller` and `classify.run_classifier` — both are real entry
points in the repo as of Phase 2 (`python -m ingest.run_poller`,
`python -m classify.run_classifier`), wrapping the importable
`ingest.poller` / `classify.outcomes` modules with production wiring
(`NTA_API_KEY`, the shared WAL-mode SQLite connection, agency scoping).

```bash
sudo cp /opt/ghost-bus/ops/ghostbus-poller.service /etc/systemd/system/
sudo cp /opt/ghost-bus/ops/ghostbus-classifier.service /etc/systemd/system/
sudo cp /opt/ghost-bus/ops/ghostbus-classifier.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

### 2.3 Enable and start

Both entry points exist in the deployed checkout, so `Restart=always` is
safe to rely on for the poller unit — a transient crash restarts into a
working process rather than spinning on an `ImportError`.

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
                                        # downloads the static GTFS zip from
                                        # transportforireland.ie, loads it via
                                        # timetable.gtfs.load_gtfs, and
                                        # prints a summary (hash, trip count,
                                        # agency names) once loaded.
sqlite3 /opt/ghost-bus/state/ghostbus.db \
  "SELECT value FROM gtfs_meta WHERE key='gtfs_hash';"   # confirm the hash changed
```

### 4.3 Disk cleanup — archives older than 7 days

Raw zstd snapshots are pruned by the `find` command below, not by any
retention logic in `ingest/poller.py` itself — the poller only ever writes
snapshots, it never deletes them. Schedule this as a cron job (it is not
wired up automatically yet); until then, run it manually if disk pressure
appears (`df -h /opt/ghost-bus`):

```bash
find /opt/ghost-bus/data -name '*.pb.zst' -mtime +7 -print -delete
```

Drop `-delete` first to dry-run and confirm the file list before deleting.

---

## 5. Upgrade to geographic progress (G1, 2026-07-19)

Deploys the spec amendment described in the README's Methodology section:
route progress is now measured by matching vehicle GPS to the trip's
nearest scheduled stop, in addition to feed `stop_sequence`. **Restart the
poller promptly after pulling.** The classifier timer fires every 10
minutes, and between `git pull` and the poller restart it can run a
classifier pass on the new code, which migrates the `observations` schema.
If that happens, the still-running old poller then crashes once on its
next write (a 5-value `INSERT` against a now-7-column table: "table
observations has 7 columns but 5 values") — `systemd`'s `Restart=always`
re-execs it straight into the pulled code, so recovery is automatic and
the cost is at most one poll's observations lost after an ok heartbeat.
This sequence starts coordinate capture soonest:

1. Pull the update:

   ```bash
   cd /opt/ghost-bus && git pull
   ```

2. Restart the poller — `init_store` adds the `lat`/`lon` columns to
   `observations` on startup, so pings only carry coordinates from this
   moment on:

   ```bash
   sudo systemctl restart ghostbus-poller.service
   ```

3. Run the timetable refresh once (step 4.2 above) to load stop
   coordinates, `stop_id`s, and route display names — geographic matching
   has no stops to match against until this has run at least once:

   ```bash
   cd /opt/ghost-bus
   .venv/bin/python -m timetable.refresh
   ```

4. Nothing else to do — `ghostbus-classifier.timer` picks up geographic
   progress on its next scheduled run automatically, no restart required.
   Optional: once burn-in data suggests a better radius than the default,
   set `GHOSTBUS_MATCH_RADIUS_M` in `/etc/ghostbus.env` (metres, default
   `250`) — the classifier is a `oneshot` unit and rereads the env file
   fresh on every run, so no service restart is needed for this to take
   effect either.

5. Time the first post-deploy classifier run as a sanity check on VM CPU —
   geographic matching adds real per-trip compute (haversine over every
   ping x every stop) that the pre-G1 classifier never paid for:

   ```bash
   journalctl -u ghostbus-classifier.service -n 20 --no-pager
   ```

   Compare the timestamp gap between the unit's start and finish log lines
   against pre-G1 runs. A run stretching well past the classifier's own
   10-minute timer period on the free-tier VM is the first sign the match
   radius or trip volume needs attention: outcome writes are buffered until
   after classification finishes (so a slow pass no longer holds a write
   lock open against the poller), but the classification pass itself is
   still single-threaded on shared CPU and a long-running geo-match sweep
   is still worth catching early.

---

## 6. Burn-in measurement: feed staleness (vehicle_ts vs ts_utc)

> Schema addition only — **not** a spec amendment. The classification
> methodology is unchanged since G1; this section adds a measurement whose
> results may *later* justify an amendment.

Every VehiclePositions ping stores two clocks: `ts_utc` (when *we* polled)
and `vehicle_ts` (when the *vehicle* says it reported). Their difference is
the feed's republication lag. This matters because the classifier's 10-minute
COMPLETED branch treats any position in the window as evidence the bus was
moving — if NTA republishes a frozen position for a bus that went silent,
that reads as a live bus, and the error is operator-flattering.

**Nothing classifies on this yet** (see the README's Known limitations). The
purpose of this section is to produce the distribution that would justify a
threshold. Deploy note: `vehicle_ts` is NULL for every row written before this
upgrade, and NULL is not evidence of freshness — the coverage columns below
exist so a partly-migrated database can't be mistaken for a fresh-feed result.

### 6.1 Lag distribution

```bash
sqlite3 /opt/ghost-bus/state/ghostbus.db <<'SQL'
WITH lag AS (
  SELECT CAST(ROUND((julianday(ts_utc) - julianday(vehicle_ts)) * 86400.0) AS INTEGER) AS s
  FROM observations
  WHERE kind = 'position' AND vehicle_ts IS NOT NULL
)
SELECT
  (SELECT COUNT(*) FROM observations WHERE kind='position')                       AS positions,
  (SELECT COUNT(*) FROM lag)                                                      AS with_vehicle_ts,
  (SELECT MIN(s) FROM lag)                                                        AS min_s,
  (SELECT s FROM lag ORDER BY s LIMIT 1 OFFSET (SELECT COUNT(*)/2     FROM lag))  AS p50_s,
  (SELECT s FROM lag ORDER BY s LIMIT 1 OFFSET (SELECT COUNT(*)*9/10  FROM lag))  AS p90_s,
  (SELECT s FROM lag ORDER BY s LIMIT 1 OFFSET (SELECT COUNT(*)*99/100 FROM lag)) AS p99_s,
  (SELECT MAX(s) FROM lag)                                                        AS max_s;
SQL
```

Reading the output:

- `with_vehicle_ts` well below `positions` after a full day of post-upgrade
  polling means the feed omits vehicle timestamps for some operators — that
  is itself a finding, and it caps how much of the fleet any staleness rule
  could ever cover.
- `p50_s` is the feed's normal republication lag. Expect tens of seconds; our
  own poll cadence (60 s, each endpoint sampled every 120 s) is inside this
  number, so a small positive median is healthy, not stale.
- `p90_s`/`p99_s`/`max_s` are where frozen positions would show up. A long
  tail — positions minutes old still being served — is the signal that the
  COMPLETED branch is crediting stale evidence.
- **Negative `min_s` means vehicle clock skew, not staleness** (the bus
  claims to have reported after we fetched). A few seconds is unremarkable;
  large negatives mean `vehicle_ts` is unreliable for those vehicles and any
  future threshold must tolerate them.

### 6.2 Per-day trend

One run of the above pools every day since the upgrade. Split it by day
before drawing conclusions — a single outage day can dominate the tail:

```bash
sqlite3 /opt/ghost-bus/state/ghostbus.db <<'SQL'
SELECT substr(ts_utc,1,10) AS day,
       COUNT(*)                                                                   AS positions,
       SUM(vehicle_ts IS NOT NULL)                                                AS with_vehicle_ts,
       CAST(ROUND(AVG(CASE WHEN vehicle_ts IS NOT NULL
            THEN (julianday(ts_utc)-julianday(vehicle_ts))*86400.0 END)) AS INTEGER) AS mean_lag_s
FROM observations
WHERE kind = 'position'
GROUP BY day ORDER BY day;
SQL
```

Mean is deliberately the coarse first cut here — it is cheap and a jump in
it is a reliable "go look at §6.1 for that day" trigger. Do not publish the
mean itself: staleness is a tail phenomenon and the mean hides the tail.

---

## 7. Backfilling GPS coordinates from the archive (`ingest/backfill.py`)

The first ~1.5 days of burn-in (before G1's poller restart, §5) captured raw
`state/archive/YYYYMMDD/HHMMSS.pb.zst` snapshots without ever writing
`lat`/`lon` into `observations` — coordinate capture didn't exist yet, but the
positions were in the feed all along, sitting compressed in the archive. This
one-off tool replays those snapshots and fills the gap. It also backfills
`vehicle_ts` on any row still missing it, for the same reason (that column was
a still-later schema addition — §6), and degrades cleanly on a database that
predates that migration.

**Always dry-run first and read the counters before `--apply`:**

```bash
cd /opt/ghost-bus
.venv/bin/python -m ingest.backfill                 # dry run, whole archive
.venv/bin/python -m ingest.backfill --day 20260717   # dry run, one day only
```

Add `--apply` only once the dry-run counters look right:

```bash
.venv/bin/python -m ingest.backfill --apply
```

### 7.1 Reading the counters

The tool prints two summary lines. The second one is the one to read
carefully:

```
snapshots read <N> (unreadable <N>); coordinate pings <N>
filled <N>; already had coordinates <N>; no stored observation <N>; ambiguous <N>
```

- **filled** — pings that wrote (or, in dry-run, would write) at least one
  new column value. This is the number that should be large on the first
  `--apply` run and drop to (near) zero on every run after.
- **already had coordinates** — pings that wrote nothing because the
  matching row already held every column this run can fill, or this ping
  simply had no value to offer for whatever was still missing (a vehicle
  that never reports its own timestamp can never advance `vehicle_ts`, and
  that has to read as "nothing to do", not "still fillable forever"). A
  second `--apply` pass over the same archive should land almost entirely
  in this bucket.
- **no stored observation** — a usable position ping in the archive with no
  matching row in `observations` at all. Expected in modest numbers (a poll
  that got archived but whose parse the live poller itself skipped for an
  unrelated reason); a number close to **coordinate pings** means the join
  key is broken and the run needs to stop before `--apply`.
- **ambiguous** — pings whose join key matched *more than one* stored row.
  This means two vehicles reported the same `trip_id` in the same snapshot;
  this tool's key (`trip_id`, `service_date`, second-resolution `ts_utc`)
  cannot tell them apart, and writing anyway would risk pinning one
  vehicle's real coordinates onto the other's row — a wrong coordinate,
  which is worse than the missing one this tool exists to fix. Neither
  candidate row is touched, and this counter is where that fact stays
  visible instead of being silently absorbed into "already had
  coordinates". **A nonzero `ambiguous` count is not a bug in this tool —
  it is the tool refusing to guess.** It should stay small; if it is a
  large fraction of `coordinate pings`, treat that as a feed data-quality
  finding worth its own investigation, not something to work around here.
- **unreadable** — snapshot files that failed to decompress or parse (a
  truncated zstd frame, a gateway error page the poller archived before the
  parse guard existed, or an unrecognisable filename). Each one is printed
  to stderr with the file path and the exception repr as it's counted, so a
  spike here is diagnosable rather than an opaque number — check whether
  it's a handful of known-corrupt files (fine) or a new failure mode (not
  fine, look at the printed exceptions).

### 7.2 Safety

**It never overwrites a value that is already set**, column by column — not
just row by row. If a row already has `lat`/`lon` from live capture but is
still missing `vehicle_ts` (a row from the window between the G1 and
`vehicle_ts` deploys), this tool fills only `vehicle_ts` and leaves the
existing coordinates untouched; the reverse is equally true. This holds in
both dry-run and `--apply`, and it's how the second-run-is-a-no-op guarantee
above works.

**It is safe to run against the live, actively-polled database.** Every
snapshot it replays is already in the past by construction (its key comes
from an archived file's own timestamp), so it can never collide with the
live poller's `INSERT`s, which always stamp the current moment. Each
archive file commits in its own short transaction, so it never holds a
write lock open long enough to starve the poller or the classifier. No
service needs to be stopped for this to run. That said, treat `--apply` as
you would any bulk write: run the dry-run first, and prefer running it when
you can watch `journalctl -u ghostbus-poller.service` afterward to confirm
nothing regressed.
