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

Before installing any credential into this file (including `GHOSTBUS_PUBLISH_TOKEN`
in Task 19's publisher setup), check for a persistent git credential cache in
every scope that could matter. `GIT_ASKPASS` only answers git's password
prompt — it does not stop git from also handing a successful credential to
every configured `credential.helper`'s `store` action afterward. `publish.sh`
already neutralises this in code (`GIT_CONFIG_NOSYSTEM=1` plus
`-c credential.helper=` on every network-touching git command), so this is
belt-and-braces, not the primary defence — and it must inspect the account
that actually runs `ghostbus-publisher.service`: **`ubuntu`** (`User=ubuntu`
in the unit — chosen over the systemd default of root specifically so the
checkout's owner matches the process's UID and to shrink the blast radius of
the one credential on this VM that can write to GitHub; see the unit file's
own comment). Check both the `ubuntu` account and system scope, since either
could still carry a stale helper from before this was set up:

```bash
git config --global --list --show-origin | grep -i credential   # as ubuntu; expect: no output
sudo git config --system --list --show-origin | grep -i credential   # expect: no output
```

If anything prints, find and unset it in the scope shown — e.g.
`git config --global --unset credential.helper` (as `ubuntu`) or
`sudo git config --system --unset credential.helper`.

### 2.1a Dubious ownership on the publisher's checkouts

**"detected dubious ownership in repository"** from git, immediately after
install or after any manual `chown`/`chmod` on `/opt/ghost-bus` or
`/opt/ghost-bus/data-repo`. `User=ubuntu` (above) means this should not occur
as long as both checkouts stay owned by `ubuntu` — if it does anyway, fix the
ownership (`sudo chown -R ubuntu:ubuntu /opt/ghost-bus`) rather than adding a
`safe.directory` exception; the latter is the standard git remedy in general,
but here it would paper over an ownership mismatch that shouldn't exist in
the first place, and `GIT_CONFIG_NOSYSTEM=1` in `publish.sh` means a
`safe.directory` entry in *system* scope wouldn't even be read anyway (it
would have to go in `ubuntu`'s own `~/.gitconfig`).

### 2.2 Install the systemd units

`ghostbus-poller.service` and `ghostbus-classifier.service` `ExecStart` into
`ingest.run_poller` and `classify.run_classifier` — both are real entry
points in the repo as of Phase 2 (`python -m ingest.run_poller`,
`python -m classify.run_classifier`), wrapping the importable
`ingest.poller` / `classify.outcomes` modules with production wiring
(`NTA_API_KEY`, the shared WAL-mode SQLite connection, agency scoping).

**All three units — poller, classifier, and publisher — run as `ubuntu`
(`User=ubuntu`), deliberately.** This is a decision, not an oversight: do not
"fix" one of them back to root, even if it looks unnecessary in isolation.
`User=ubuntu` started on the publisher specifically to shrink the blast
radius of the one credential able to write to GitHub, and was extended to
all three so that every process touching the shared `state/ghostbus.db`
(WAL mode) has the same owner as the checkout itself — a live test showed
an `ubuntu` reader could in fact read a database with root-owned `-wal`/`-shm`
files being actively written by a root poller, but resting the one
GitHub-writing component's correctness on an unexplained, empirically
observed (not verified-by-understanding) cross-UID access pattern was judged
not worth it. One owner removes the question.

```bash
sudo cp /opt/ghost-bus/ops/ghostbus-poller.service /etc/systemd/system/
sudo cp /opt/ghost-bus/ops/ghostbus-classifier.service /etc/systemd/system/
sudo cp /opt/ghost-bus/ops/ghostbus-classifier.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

**On an existing install** (poller/classifier already running as root before
this change), a one-time ownership fix is required — and is not, by itself,
enough:

```bash
sudo systemctl stop ghostbus-poller.service ghostbus-classifier.service
sudo chown -R ubuntu:ubuntu /opt/ghost-bus/state
```

The one-shot `chown` fixes the files that exist *right now*. It is not
sufficient on its own: the poller has been recreating root-owned
`state/*` files (the SQLite file itself, and its `-wal`/`-shm` companions)
on every restart, since it ran as root and any file it creates is
root-owned. Installing the updated unit (with `User=ubuntu`) and reloading
systemd — the steps above and below — is what makes the ownership actually
stick, by changing which account creates those files going forward.

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
find /opt/ghost-bus/state/archive -name '*.pb.zst' -mtime +7 -print -delete
```

Drop `-delete` first to dry-run and confirm the file list before deleting.

### 4.4 Publisher refuses every run: stray file in data-repo

`ghostbus-publisher.service` failing every night with `publish: staged files
outside the dataset contract - refusing to publish` (check
`journalctl -u ghostbus-publisher.service`). `publish.sh`'s fetch+reset step
(`git reset --hard`) restores tracked files to match the remote, but does
**not** remove untracked ones — so a stray untracked file left inside
`data/daily` or `data/uptime` (a half-written file from an interrupted run,
manual debugging, or worse) persists across runs and trips the abort gate
every night, indefinitely. This is the correct, safe behaviour — refusing to
publish beats guessing — but it needs a human to clear it:

```bash
cd /opt/ghost-bus/data-repo
git status --porcelain -- data/daily data/uptime   # find the offending file(s)
git clean -n -- data/daily data/uptime             # dry run - confirm what would go
git clean -f -- data/daily data/uptime             # remove it, then re-run the timer
```

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

   **This runs against the live, actively-polled database, and that is
   expected — do not stop the poller first.** `timetable.refresh` loads a
   full national `stop_times` table (~5.9M rows) as one DELETE + reinsert
   transaction, deliberately atomic so a half-loaded timetable is never
   visible, and that holds SQLite's write lock for the duration of the
   reload (minutes, not seconds). For that whole window the poller's own
   writes will hit "database is locked"; as of the lock-contention fix in
   `ingest/poller.py`, it now handles this correctly on its own — it logs
   the condition to stderr and skips the poll instead of crashing, and
   `systemd` never sees a process death, so there is no restart storm to
   compound the outage. Each skipped poll shows up as a missing heartbeat,
   which is honest tracker downtime: it counts toward the same 90%-uptime
   threshold `classify/outcomes.py` (documented on the methodology page)
   uses to decide EXCLUDED, for every trip window it falls inside. If enough
   of this outage lands inside a trip's own window to push that trip's
   uptime below 90%, the trip is EXCLUDED from operator statistics rather
   than counted against the operator. But the threshold cuts both ways: a
   refresh short enough, or timed such that no single trip window loses more
   than 10% of its own coverage to it, leaves every affected trip judged
   normally — and if the timing happens to land where a genuinely-completed
   trip would have shown its late progress, this gap is our fault, not the
   operator's, but it can still read as VANISHED on the public site. The
   poller resumes on its own the moment the refresh transaction commits and
   the lock releases — no operator action needed. (Stopping the poller for
   the refresh and restarting it afterwards would also be honest, but adds a
   manual restart step that is easy to forget and would silently extend the
   downtime indefinitely if missed — strictly worse than the automatic,
   self-healing skip-and-resume behavior above.)

4. Nothing else to do — `ghostbus-classifier.timer` picks up geographic
   progress on its next scheduled run automatically, no restart required.
   Optional: once burn-in data suggests a better radius than the default,
   set `GHOSTBUS_MATCH_RADIUS_M` in `/etc/ghostbus.env` (metres, default
   `250`) — the classifier is a `oneshot` unit and rereads the env file
   fresh on every run, so no service restart is needed for this to take
   effect either. **If you change this value, update the "250 metres today"
   figure in `site/methodology.html.tmpl` in the same change** — CI only
   pins the code's *default* (`DEFAULT_MATCH_RADIUS_M`), not this env
   override, so a retune here leaves the published methodology page wrong
   with no test to catch it.

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

> **Update 2026-07-22: the amendment this section existed to justify now
> exists — G2, the evidence clock** (design:
> `docs/superpowers/specs/2026-07-22-staleness-design.md`; deploy: §9).
> The classifier times position evidence by `min(vehicle_ts, ts_utc)`.
> These queries stay: they are the ongoing monitoring that G2's two
> preconditions (near-total `vehicle_ts` coverage, no negative skew) still
> hold, and §6.4 watches the one residual risk G2 introduced.

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

### 6.3 Feed-volume watch (VehiclePositions degradation)

> Added 2026-07-22 after a real incident: on 2026-07-21 ~19:20–20:00 UTC the
> VehiclePositions feed partially collapsed (Dublin Bus −79%, Bus Éireann
> −80%, Go-Ahead −19% at the trough) while TripUpdates and our own heartbeats
> ran normally. The classifier converted the gap into ~280 false VANISHED
> verdicts. **The §3 health checks cannot see this** — they watch *our*
> uptime, and we were up. This section is the detection query; whether the
> classifier should act on feed degradation is a separate design decision
> (`docs/superpowers/specs/2026-07-22-feed-health-design.md`, proposed G3).

Position pings per 10-minute bucket for a suspect window, against the same
buckets one day earlier — run when a day shows an unexplained VANISHED spike,
or daily during burn-in:

```bash
sqlite3 -readonly /opt/ghost-bus/state/ghostbus.db <<'SQL'
SELECT substr(ts_utc,1,15) || '0' AS bucket10,
       SUM(kind='position') AS positions,
       SUM(kind='update')   AS updates
FROM observations
WHERE ts_utc >= '2026-07-21T17:00' AND ts_utc < '2026-07-21T22:00'
GROUP BY bucket10 ORDER BY bucket10;
SQL
```

Reading it: positions decline smoothly through an evening (07-20 ran ~6.7k
→ ~3.6k over 17:30–21:20 with no bucket below ~2.9k). A **V-shape** — a
sharp dip with recovery — is a feed event, not service wind-down. `updates`
holding steady while `positions` collapses is the confirming signature: the
predictions pipeline is fine, the vehicle-telemetry pipeline is not.

Attribute a dip to operators before drawing any conclusion — simultaneous
collapse across operators means a shared upstream, not mass breakdowns:

```bash
sqlite3 -readonly /opt/ghost-bus/state/ghostbus.db <<'SQL'
SELECT substr(o.ts_utc,1,15) || '0' AS bucket10,
       COALESCE(a.agency_name,'(other)') AS agency,
       COUNT(*) AS positions
FROM observations o
LEFT JOIN gtfs_trips t ON t.trip_id = o.trip_id
LEFT JOIN gtfs_routes r ON r.route_id = t.route_id
LEFT JOIN gtfs_agency a ON a.agency_id = r.agency_id
WHERE o.kind='position' AND o.ts_utc >= '2026-07-21T18:50'
                        AND o.ts_utc <  '2026-07-21T20:20'
GROUP BY bucket10, agency ORDER BY bucket10, positions DESC;
SQL
```

Any interval this section flags contaminates that day's VANISHED counts —
record it in the vault's KNOWN_ISSUES and treat the day as unpublishable
until the G3 decision exists. Use `-readonly` as written: these are
analysis queries and must never hold a write lock on the live database.

### 6.4 Frozen-clock watch (the residual risk G2 introduced)

G2 trusts `vehicle_ts`. The failure mode that would hurt an operator: a
vehicle whose **clock freezes while its GPS keeps updating** — its pings
would look increasingly stale, the evidence clock would stop advancing, and
a live bus could read as VANISHED. The signature is identical `vehicle_ts`
across pings whose *positions differ* (identical position + identical
vehicle_ts is ordinary republication, which G2 handles by design):

```bash
sqlite3 -readonly /opt/ghost-bus/state/ghostbus.db <<'SQL'
SELECT substr(ts_utc,1,10) AS day,
       COUNT(*) AS frozen_groups,
       SUM(n_pings) AS pings_involved
FROM (
  SELECT ts_utc, COUNT(*) AS n_pings
  FROM observations
  WHERE kind='position' AND vehicle_ts IS NOT NULL
        AND lat IS NOT NULL AND lon IS NOT NULL
  GROUP BY trip_id, service_date, vehicle_ts
  HAVING COUNT(DISTINCT lat || ',' || lon) > 1
)
GROUP BY day ORDER BY day;
SQL
```

Caveats when reading it: two physically distinct vehicles reporting the
same trip_id (the backfill's ambiguity case — it has happened in
production) also produce differing positions under one timestamp, so treat
single groups as noise and *trends* as signal. A material, persistent rate
here reopens the G2 design (the hybrid floor discussed in its "residual
risks" section) — in the spec and on the methodology page, not as a quiet
code change.

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
- **ambiguous** — pings whose join key is not unique. This now covers three
  cases, all refused the same way: writing would risk pinning one vehicle's
  real coordinates onto the wrong row — a wrong coordinate, which is worse
  than the missing one this tool exists to fix — so none of the colliding
  candidates are touched. **Every one of the three cases is now printed to
  stderr, one line per distinct ambiguous key** (not one per ping, so a
  pathological snapshot can't flood the log) — a nonzero `ambiguous` count
  is traceable to the trip that caused it without hand-writing a query
  against `observations`.
  - *Two or more pings in the same snapshot* share a key — two vehicles
    reported the same `trip_id` in the same poll, and this tool's key
    (`trip_id`, `service_date`, second-resolution `ts_utc`) cannot tell them
    apart. This is a feed anomaly — genuinely ambiguous source data, nothing
    wrong in `observations` itself. Printed as:
    `ambiguous ping trip_id=<id> service_date=<date> ts_prefix=<ts_prefix>:
    <N> pings in this snapshot share this join key - refusing to guess which
    is which`.
  - Rarer: a single ping's key matches more than one stored row already in
    `observations`. Unlike the case above, the snapshot itself is
    unambiguous — the duplication is in the stored rows, which usually means
    something upstream of this tool double-wrote a row for that key and is
    worth its own look. Printed as:
    `ambiguous ping trip_id=<id> service_date=<date> ts_prefix=<ts_prefix>:
    matches <N> stored rows for this key - refusing to guess which one`.
  - *Two different archive files resolve to the same `ts_prefix`* — the walk
    is recursive, so this can happen from anywhere in the tree (a
    per-endpoint subdirectory, a copied day directory, a partial restore
    placed alongside the original), not just two entities inside one
    snapshot. Every ping in every file sharing that prefix is counted
    ambiguous, and each colliding path is printed to stderr on its own line:
    `ambiguous snapshot <path>: shares timestamp <ts_prefix> with <other
    path(s)> - refusing to guess which file's pings belong to which row`.

  This is deliberately independent of how many stored rows currently exist
  for the key: an interrupted poll can leave the archive ahead of the
  database (see "no stored observation" above), and a guard that only looked
  at stored-row count would let the first of two colliding pings write, then
  silently absorb the second into "already had coordinates" once its probe
  saw the first one's own update. This counter is where the collision stays
  visible instead. **A nonzero `ambiguous` count is not a bug in this tool —
  it is the tool refusing to guess.** It should stay small; if it is a large
  fraction of `coordinate pings`, treat that as a feed data-quality finding
  worth its own investigation (or, for the cross-file case, a stray copy in
  the archive tree worth cleaning up), not something to work around here. The
  stderr wording above tells you which of the two in-file cases you're
  looking at without writing SQL: "pings in this snapshot share this join
  key" means look at the feed for that timestamp; "matches N stored rows"
  means look at `observations` for that key.

  Refused files are still counted in `snapshots read` — they were opened and
  parsed, only not written from. `snapshots read` plus `unreadable` should
  always equal the number of `*.pb.zst` files under the archive path; if it
  does not, that is a bug in this tool, not a data condition.

  Do not leave symlinks or directory junctions inside `state/archive`: the
  walk would see one physical snapshot under two paths, read them as a
  cross-file collision, and refuse both. Safe, but it costs real fills for
  no reason.
- **unreadable** — snapshot files that failed to decompress or parse (a
  truncated zstd frame, a gateway error page the poller archived before the
  parse guard existed) or whose filename could not be parsed into a
  timestamp at all. The filename check requires an exact `HHMMSS.pb.zst`
  name, so a stale backup, numbered copy, or in-progress-write left in the
  archive tree (`215141.bak.pb.zst`, `215141.1.pb.zst`, `215141.tmp.pb.zst`)
  lands here too, rather than being decoded and silently resolving to the
  same timestamp as the real file. Each one is printed to stderr with the
  file path as it's counted — the decompress/parse failures also include the
  exception repr, the unrecognisable-filename case states plainly that the
  filename couldn't be parsed — so a spike here is diagnosable rather than an
  opaque number — check whether it's a handful of known-corrupt or stray
  files (fine) or a new failure mode (not fine, look at the printed
  exceptions).

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

---

## 8. Publishing (dataset -> GitHub Pages)

Publishing is split in two on purpose. The **VM** produces the dataset and
pushes it to a *separate* repository, `aleks-drozy/ghost-bus-data`, which holds
nothing but CSVs and a manifest. **CI** checks that repository out beside the
code, renders the site from those CSVs, and deploys it.

### 8.0 Upgrading an existing install to P4 (poller + classifier + publisher all as `ubuntu`)

Do this section first, top to bottom, on any VM that installed the poller
and classifier before `User=ubuntu` existed for them (§2.2), before working
through the rest of §8 below. §2.2's own upgrade block stops the poller and
classifier and `chown`s `state/` but never starts them back up — it is
written for a fresh install, where §2.3 is what does the starting — so
followed on its own on an *existing* install it leaves both services down.
And §8.2 never mentions the poller/classifier `User=ubuntu` change or its
`chown` at all: an operator who follows §8 end to end without this section
gets a publisher running as `ubuntu` while the poller and classifier stay
root, recreating root-owned `state/*` files forever — exactly the mixed-UID
situation `User=ubuntu` was introduced to remove.

1. **Pull first — the order matters.** The `cp` in the next step copies
   whatever unit files are already sitting on disk. Run it before this pull
   and it (re)installs the OLD root-owned units from the previous checkout,
   silently undoing the rest of this section.

   ```bash
   cd /opt/ghost-bus
   git pull
   ```

2. **Copy all five unit files** — poller, both classifier units, and both
   publisher units — not just whichever one seems to have changed, so none
   is left stale:

   ```bash
   sudo cp /opt/ghost-bus/ops/ghostbus-poller.service /etc/systemd/system/
   sudo cp /opt/ghost-bus/ops/ghostbus-classifier.service /etc/systemd/system/
   sudo cp /opt/ghost-bus/ops/ghostbus-classifier.timer /etc/systemd/system/
   sudo cp /opt/ghost-bus/ops/ghostbus-publisher.service /etc/systemd/system/
   sudo cp /opt/ghost-bus/ops/ghostbus-publisher.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

3. **Stop the poller and classifier before touching ownership** — never
   `chown` files a running process still has open for write:

   ```bash
   sudo systemctl stop ghostbus-poller.service ghostbus-classifier.service
   ```

4. **`chown` the whole tree, not just `state/`.** §2.2's own one-time fix
   names only `/opt/ghost-bus/state`, because on a fresh install nothing
   else under `/opt/ghost-bus` is root-owned yet. An existing install being
   brought up to P4 can also have a root-owned `data-repo/` checkout or
   stray root-owned files elsewhere in the tree from earlier manual
   debugging, and every one of them has to end up `ubuntu`-owned — a
   root-owned `data-repo/` refuses `git` operations for the `ubuntu`-run
   publisher exactly as a root-owned `state/` refuses the poller:

   ```bash
   sudo chown -R ubuntu:ubuntu /opt/ghost-bus
   ```

5. **Start the poller and classifier back up:**

   ```bash
   sudo systemctl start ghostbus-poller.service
   sudo systemctl start ghostbus-classifier.timer
   ```

6. **Verify — do not assume the unit change and the `chown` actually took:**

   ```bash
   systemctl show -p User ghostbus-poller.service      # expect: User=ubuntu
   ls -l /opt/ghost-bus/state                           # expect: every file owned by ubuntu
   ```

   A `state/*` file still owned by `root` here means a unit was copied
   before the `git pull` in step 1, or `daemon-reload` was skipped — stop
   the affected service, redo steps 1-4, and check again before continuing.

Continue into §8.1 below — it is unaffected by whether this section has
already run, and covers the one-time GitHub-side setup (the data
repository, Pages, and the publish token) that a VM only ever needs once,
upgrade or not.

The separation is what makes the trust boundary real. A token with write
access to the *code* repository could rewrite `publish/site.py` or a template —
and CI checks the code repository out and executes it, so that is arbitrary
HTML on the public site and arbitrary code in the CI runner. Scoped to a
repository containing only data, the same permission **cannot reach**
`publish/site.py`, any template, or any test. The worst a compromised VM can
do is corrupt numbers, which are public, diffable, and recomputable.

### 8.1 One-time GitHub setup (owner only)

Three steps that cannot be scripted from the VM:

1. **Create the data repository.** A new **public** repo `aleks-drozy/ghost-bus-data`
   with a `main` branch and nothing in it but a README. Public is required,
   not just preferred: the publish workflow's `actions/checkout` step for
   this repo runs with the default `github.token`, scoped to `ghost-bus`
   alone, and cannot read a *private* `ghost-bus-data` without introducing a
   second credential — exactly what this split exists to avoid needing. It is
   also correct on the merits: it is the open dataset. The publisher writes
   `data/manifest.json`, `data/daily/`, `data/uptime/` into it. Nothing
   executable ever belongs there.
2. **Enable Pages from Actions** on the *code* repo:
   **Settings -> Pages -> Build and deployment -> Source: GitHub Actions**.
   Until this is set, every publish run fails at `actions/configure-pages`
   with `Get Pages site failed`.
3. **Mint the publish token.** A **fine-grained personal access token**, not a
   classic one:
   **Settings -> Developer settings -> Personal access tokens ->
   Fine-grained tokens -> Generate new token**
   - Resource owner: `aleks-drozy`
   - Repository access: **Only select repositories** -> `ghost-bus-data`.
     **Never `ghost-bus`.** This is the whole point of the split; a token
     that can also write the code repo gives back everything the separation
     bought — see the note on `dispatches` below for exactly why.
   - Repository permissions: **Contents: Read and write**. Nothing else —
     leave Actions, Workflows, Pages, Secrets and every other permission at
     **No access**.
   - Expiration: 90 days. Put the rotation date in the calendar; §8.5 is the
     procedure.

   Copy the `github_pat_...` value once — GitHub will not show it again.

   Nothing further to configure here for triggering a rebuild. The
   **publish** workflow (`.github/workflows/publish.yml`) already rebuilds
   the site on a `schedule:` — `cron: '37 4 * * *'` UTC — timed to land after
   the VM's 03:30 Europe/Dublin publish across both the GMT and IST halves of
   the year. That is deliberate, not a stand-in for something better: the
   alternative — a workflow living in `ghost-bus-data` that fires a
   `repository_dispatch` at `ghost-bus` on every push, so a new dataset
   redeploys the site immediately — was considered and rejected, because the
   only API call that can send one (`POST /repos/{owner}/{repo}/dispatches`)
   requires **`Contents: write` on `ghost-bus` itself**. Minting that token
   just to get a dispatch sender would hand the VM exactly the capability
   this whole split exists to deny it. No dispatch sender exists anywhere in
   this project, and none should be added. §8.8 covers the one failure mode
   a schedule-only trigger cannot self-detect.

### 8.2 Install the publisher on the VM

The token goes in the same file as the NTA key, with the same permissions.
That file is the only copy of either secret on the box.

```bash
cd /opt/ghost-bus
git pull

# Clone the data repository once. The publisher writes into it and pushes it.
git clone https://github.com/aleks-drozy/ghost-bus-data.git /opt/ghost-bus/data-repo

# Append the token to the existing env file. Note the leading space: with
# HISTCONTROL=ignorespace (bash default on Ubuntu) the line stays out of
# ~/.bash_history. Paste the real token in place of github_pat_xxx.
 sudo sh -c 'printf "GHOSTBUS_PUBLISH_TOKEN=%s\n" "github_pat_xxx" >> /etc/ghostbus.env'

sudo chmod 600 /etc/ghostbus.env
sudo chown root:root /etc/ghostbus.env

# Confirm it landed without printing the value:
sudo grep -c '^GHOSTBUS_PUBLISH_TOKEN=' /etc/ghostbus.env    # expect: 1
```

**The token must never be echoed into logs.** `ops/publish.sh` never names the
variable; git receives it through `ops/git-askpass.sh`, which writes it to
git's stdin-substitute and nowhere else, so it appears neither in `ps` output
nor in the journal. The script also unsets `GIT_TRACE` and friends, so a
debugging variable left in the env file cannot leak the exchange. Do not add
`set -x` to either script, do not echo the variable while debugging, and do
not paste the value into an issue, a commit, or a chat window. If you ever see
it in `journalctl -u ghostbus-publisher.service`, treat the token as burned and
go straight to §8.5.

Install the units:

```bash
chmod +x /opt/ghost-bus/ops/publish.sh /opt/ghost-bus/ops/git-askpass.sh
sudo cp /opt/ghost-bus/ops/ghostbus-publisher.service /etc/systemd/system/
sudo cp /opt/ghost-bus/ops/ghostbus-publisher.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ghostbus-publisher.timer
systemctl list-timers ghostbus-publisher.timer --no-pager
```

The timer fires daily at **03:30 Europe/Dublin** — late enough that the
previous service day is closed and the classifier has finished with it, so the
"complete service days only" rule has a whole day to publish. `Persistent=true`
means a reboot across 03:30 runs the publish on the next boot rather than
skipping the day.

### 8.3 Verifying a publish

Run it once by hand rather than waiting for the timer:

```bash
sudo systemctl start ghostbus-publisher.service
journalctl -u ghostbus-publisher.service -n 40 --no-pager
```

A healthy run ends with one of two lines: `publish: pushed <sha>` or
`publish: dataset unchanged, nothing to push`. Then check, in order:

```bash
# 1. The dataset on the VM.
cat /opt/ghost-bus/data-repo/data/manifest.json
ls -l /opt/ghost-bus/data-repo/data/uptime | tail -5
ls -l /opt/ghost-bus/data-repo/data/daily  | tail -5     # empty before the baseline

# 2. The commit that was pushed.
cd /opt/ghost-bus/data-repo && git show --stat HEAD
```

3. On github.com, trigger the **publish** workflow directly rather than
   waiting for its nightly cron — Actions -> publish -> **Run workflow** uses
   the `workflow_dispatch:` trigger already in `.github/workflows/publish.yml`
   for exactly this. The run should go green. It runs the full test suite
   *before* it builds, so a red run means either a genuine test failure or
   that the site builder raised on real data — in both cases the previous
   site stays live and nothing is deployed.
4. **A build refusal is not a flake.** `publish/site.py` prints
   `::error::REFUSING TO BUILD: <reason>` and exits 1 on `DatasetError`,
   `OutputDirError`, or `InjectionError` — three checks against the
   *published dataset*, not against this codebase, so a red run here can mean
   `ghost-bus-data`, not the code, is at fault. `DatasetError` and
   `OutputDirError` mean the published CSVs or manifest don't match the
   contract the builder expects — go look at what was actually pushed to
   `ghost-bus-data`. **`InjectionError` means the builder caught markup in
   the published dataset that would otherwise have gone live as HTML on the
   public site — treat that one as a security event, not a broken build**,
   and find out how it got into `ghost-bus-data` before re-running anything.
5. Load `https://aleks-drozy.github.io/ghost-bus/` and confirm the uptime
   strip's latest date matches `coverage.last_day` in the manifest.

### 8.4 When the publish gate fails

This is the **dataset gate on the VM** (`publish/dataset.py` and
`run_checks.py`, run before anything is pushed) — a different check from the
CI-side build refusal in §8.3.4, which runs later, on already-published CSVs,
while rendering them. Either can fail independently of the other.

**Nothing was published, and that is the system working.** `publish.sh` runs
`publish/dataset.py` first; a failed gate exits nonzero, `set -e` stops the
script before any git command, and the run never reaches a commit. The
**previously published data stays up** — stale but verified — rather than being
replaced by numbers that failed their own checks.

You will see it as a failed unit:

```bash
systemctl --failed
journalctl -u ghostbus-publisher.service -n 60 --no-pager
```

Investigate before doing anything else:

```bash
cd /opt/ghost-bus
.venv/bin/python run_checks.py                       # which check failed, and why
.venv/bin/python -m publish.dataset --db state/ghostbus.db --data-dir /tmp/ghostbus-dryrun
```

`outcomes_valid` runs first as a gate, so if it fails, fix that before reading
anything into conservation or bounded-rates output. A conservation failure
means trips are being lost or double-counted between the timetable and the
outcomes table; a bounded-rates failure means a rate fell outside `[0, 1]`, or
a point estimate sat outside its own interval, which is a computation bug and
not a data quirk.

**Do not force a publish past a failed gate.** There is no flag for it and none
should be added: the whole value of the project is that a published number is
one the data supports. Fix the cause, re-run `.venv/bin/python run_checks.py`
until it is clean, then `sudo systemctl start ghostbus-publisher.service`.

The one *non*-failure that looks like one: a run that exits 0 saying
`dataset unchanged, nothing to push`. That is normal before the 14-day
baseline, when only the manifest and uptime CSVs exist and neither has moved.
It is **not** normal on a day when uptime should have changed — if you see it
repeatedly, check that `data/` is not being ignored by git in the data
repository.

### 8.5 Rotating the publish token

Rotate on expiry, on any suspicion of exposure, and whenever someone who has
had shell on the box no longer should.

1. Mint the replacement first, per §8.1 step 3 (same scope: `ghost-bus-data`
   only, **Contents: Read and write**, nothing else).
2. Replace the line in place, without printing it:

   ```bash
   sudo sed -i '/^GHOSTBUS_PUBLISH_TOKEN=/d' /etc/ghostbus.env
    sudo sh -c 'printf "GHOSTBUS_PUBLISH_TOKEN=%s\n" "github_pat_new" >> /etc/ghostbus.env'
   sudo chmod 600 /etc/ghostbus.env
   sudo grep -c '^GHOSTBUS_PUBLISH_TOKEN=' /etc/ghostbus.env    # expect: 1
   ```

3. `systemd` reads `EnvironmentFile` at each start of the oneshot unit, so no
   restart of a long-running process is needed — but reload and prove it works
   before you trust it:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl start ghostbus-publisher.service
   journalctl -u ghostbus-publisher.service -n 20 --no-pager
   ```

4. **Revoke the old token on github.com** (Settings -> Developer settings ->
   Fine-grained tokens -> the old token -> Delete). Rotation is not finished
   until the old one is dead — a token that still works is still a key.
5. If the rotation was triggered by suspected exposure, check the data repo's
   history for anything the old token pushed that is not a CSV or the manifest:
   `cd /opt/ghost-bus/data-repo && git log --name-only --since="30 days ago"`.
   The site builder refuses to publish an unexpected file, so such a push would
   have turned CI red rather than reaching the site — but it should still be
   found and removed.

### 8.6 Is the site in pre-baseline mode?

Pre-baseline mode is not a setting — it is a computed consequence of how many
complete service days exist. Read it from the manifest, which is the same
input the site builder uses:

```bash
python3 -c "import json;m=json.load(open('/opt/ghost-bus/data-repo/data/manifest.json'));print(m['scoreboard_ready'], m['coverage']['complete_days'], m['baseline_required_days'])"
```

- `False 9 14` -> pre-baseline. `data/daily/` is empty by design, the site
  renders methodology, the uptime strip, and "collecting baseline — day 9 of
  14". No route table, no route pages. **This is correct behaviour, not an
  outage.** Uptime is deliberately exempt from the gate and publishes from day
  one: it is our own downtime, not a claim about any operator.
- `True 14 14` -> the scoreboard is live. `data/daily/` has one CSV per
  complete service day and `index.html` carries the ranked table.

The gate is a state, not an event: if coverage ever falls back below 14 days,
the publisher **withdraws** the route CSVs and the site returns to pre-baseline
mode. That is intended. A site saying "we publish nothing about any route" must
not be linking route data.

From the outside, without shell access: fetch
`https://aleks-drozy.github.io/ghost-bus/data/manifest.json`, or just look at
the front page — pre-baseline mode says so in plain English on the page.

### 8.7 Publishing from a fresh checkout after a VM rebuild

The VM's `data-repo` is a working copy of what is already public, so a rebuilt
box does not need any of it restored: `git clone` brings the published dataset
back, and the next publish adds to it. The only things that must be recreated
by hand are the clone (§8.2) and `/etc/ghostbus.env` (§2.1 for the NTA key,
§8.2 for the publish token).

### 8.8 Detecting a silently stalled scheduled rebuild

The nightly rebuild (§8.1) depends on GitHub continuing to fire the
`schedule:` trigger, and that is not guaranteed forever: GitHub **auto-disables
scheduled workflows in a public repository after 60 days with no repository
activity** — and `ghost-bus`'s own day-to-day activity happens in
`ghost-bus-data`, not here, precisely because that is the point of the split.
A `ghost-bus` codebase that goes two quiet months without a commit (entirely
plausible once it settles) can have its schedule silently disabled with no
error and no failed run to alert on. A run that never fires cannot fail, so
nothing here notices on its own.

Check for this directly first: on github.com, **Actions -> publish**. A
disabled scheduled workflow shows a banner with a re-enable control on that
page (or run `gh workflow enable publish.yml` from the CLI). Re-enabling only
resumes the schedule going forward — it does not itself trigger the runs that
were missed — so follow it with a manual **Run workflow** (§8.3) to catch the
site up immediately.

Absent that, watch it from the output side instead:

- **`generated_at`**, on the published about-data page
  (`https://aleks-drozy.github.io/ghost-bus/about-data.html`) or directly in
  `data/manifest.json`, should never be more than about a day old. A value
  that keeps falling further behind `date -u` means the rebuild has stopped —
  even while `ghostbus-publisher.service` keeps publishing a fresh dataset to
  `ghost-bus-data` every night without complaint.
- An **external staleness check**, outside GitHub entirely, closes the loop
  without depending on a human remembering to look — the VM already has a
  `healthchecks.io` account wired up for the poller (§3.3); a second, longer-
  period check that fetches the manifest and pings only when `generated_at`
  is fresh is the natural place to add this.

---

## 9. Upgrade to the evidence clock (G2, 2026-07-22)

> Methodology amendment — after this deploy, COMPLETED's time branch and
> VANISHED's cutoff read the vehicle's own report time
> (`min(vehicle_ts, ts_utc)`), and the G1 pre-start progress gate does the
> same. No schema change, no config change, no unit-file change: the deploy
> is a code pull plus one deliberate reclassification pass.

**Blocked until the P4 owner steps (§8.1) are done and main is pushed** —
the VM pulls from the public repo.

### 9.1 Deploy

As `ubuntu` (every unit runs as `ubuntu` — §2.2 — and the checkout is
`ubuntu`-owned; no `sudo` anywhere in this section):

```bash
cd /opt/ghost-bus && git pull
# classifier picks the new code up on its next timer run; nothing to restart
# (the poller does not classify, no schema changes, no unit-file changes).
```

### 9.2 Reclassify the whole baseline under one methodology

`classify_day` upserts by `(trip_id, service_date)` and observations are
never deleted, so reclassification is just re-running the classifier over
every burn-in date. Run it once, immediately after the deploy, so the
published series never mixes pre- and post-G2 verdicts.

**Source the environment file first — this bit its own author.** The
systemd units get `GHOSTBUS_AGENCIES` from `EnvironmentFile=`; a manual
shell does not, so without sourcing, `read_agency_names()` silently falls
back to the default `"Dublin Bus"` — which matches NOTHING on this feed
(the agency is `"Bus Átha Cliath – Dublin Bus"`), and the run quietly
reclassifies only Go-Ahead, leaving the fleet's majority on the old
methodology. The assert below turns that silent miss into a loud stop:

```bash
cd /opt/ghost-bus && set -a && . <(sudo cat /etc/ghostbus.env) && set +a && \
.venv/bin/python - <<PY
import datetime as dt
from classify.run_classifier import run_for_dates
from classify.store import init_store
from ghostbus_config import get_db, read_agency_names, read_match_radius_m

names = read_agency_names()
assert any("tha Cliath" in n for n in names), f"agency env not loaded: {names}"
db = get_db(); init_store(db)
start = dt.date(2026, 7, 18)          # first burn-in service date
end = dt.date.today()
dates = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
summary = run_for_dates(db, dates, names,
                        dt.datetime.now(dt.timezone.utc), read_match_radius_m())
for day, counts in sorted(summary.items()):
    print(day, dict(sorted(counts.items())))
PY
```

The classifier writes here, so `-readonly` does not apply — but the same
lock courtesy as the nightly timer does: `classify_day` computes read-only
and batches its writes at the end, and its own write burst is the same size
as any normal run.

Expected direction of movement, from the design's measurement: a small
number of COMPLETED trips (order tens per day, concentrated in late-evening
hours) become VANISHED; **no trip can move toward COMPLETED** — G2 is
monotonically stricter by construction. A movement wildly outside that
envelope (hundreds per day, or any COMPLETED gain) means something is wrong:
stop and compare against the pre-deploy `trip_outcomes` before publishing
anything.

### 9.3 Verify

- Re-run a spot date's counts twice — identical output (idempotence).
- §6.1 coverage columns still ~100% and `min_s` still non-negative (G2's
  preconditions).
- §6.4 frozen-clock watch shows no material trend.
- The 2026-07-21 feed-degradation day (§6.3) remains contaminated
  regardless of G2 — its handling is the G3 decision, not this one.

---

## 10. Upgrade to the feed-health gate (G3, 2026-07-22)

> Methodology amendment, deployed together with §9 (G2): both change
> classification only, so one `git pull` and ONE reclassification pass
> covers the pair. No schema change, no config change, no unit-file change,
> no new index - detection reuses the same per-trip index classification
> already uses.

**Blocked until the P4 owner steps (§8.1) are done and main is pushed.**

### 10.1 Deploy

Covered by §9.1's `git pull`. The classifier's next timer run computes
feed-health shields automatically; `run_for_dates` wires them in.

### 10.2 Reclassify

Run §9.2 once, after pulling code that contains BOTH amendments - do not
run it between them. Expected additional movement from G3: on
**2026-07-21** roughly 200-300 VANISHED/UNTRACKED trips in the
18:00-20:30 window become EXCLUDED_FEED (the incident that motivated the
amendment); other days should move little or not at all. That day is
withdrawn from publication regardless (WITHDRAWN_DAYS in
publish/dataset.py), so the reclassification is for the database's own
honesty, not for the published record.

### 10.3 Verify

- `run_checks.py` passes (conservation now sums six classes).
- Spot-check 2026-07-21 evening:

```bash
sqlite3 -readonly /opt/ghost-bus/state/ghostbus.db "
SELECT outcome, COUNT(*) FROM trip_outcomes
WHERE service_date='2026-07-21' GROUP BY outcome;"
```

  EXCLUDED_FEED should be in the low hundreds; VANISHED should fall back
  to the same order as the surrounding days (60-120).
- The published dataset shows no `daily/2026-07-21.csv`, and the manifest
  lists the day under `withdrawn_days` with its reason.
- §6.3's feed-volume watch remains the *diagnostic* view; the gate is the
  *automatic* consequence. If §6.3 ever shows a V-shape the gate did not
  catch, that is a parameter-fit finding - record it in the vault and
  revisit the constants by commit, not by env var.
