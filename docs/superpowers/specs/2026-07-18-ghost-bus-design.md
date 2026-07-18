# Dublin Ghost Bus Tracker — Design

**Date:** 2026-07-18 · **Status:** approved pending user spec review
**Repo:** `ghost-bus` (public GitHub, `aleks-drozy`) · Site: GitHub Pages
**Mission:** measure, honestly and continuously, how often Dublin's scheduled
buses never show up — and publish it where commuters can use it.

## What it is

A 24/7 pipeline that polls TFI's GTFS-Realtime feed once a minute, matches
observations against the published GTFS timetable, classifies every scheduled
trip into exactly one outcome, and publishes a public scoreboard + open dataset.
The methodology page and the tracker's own uptime self-report are first-class
features: we are publicly grading a state service, so our own measurement
honesty must be beyond reproach.

## Scope

**In (v1):** Dublin Bus + Go-Ahead Ireland Dublin routes; trip-level outcome
classification; route/hour/day aggregates; ghost-rate scoreboard site; daily
open CSVs; pipeline health monitoring with public uptime self-report.
**Out (v1):** Bus Éireann, rail, Luas; stop-level arrival predictions;
historical data before go-live; a native app; any claim about *why* a bus
ghosted.

## Data sources

- **Static timetable:** TFI operator GTFS zips from data.gov.ie
  ("Operator GTFS Schedule files"). Re-fetched weekly (and on trip-match
  failures spiking); each version stored with hash + validity window so every
  classification is traceable to the timetable version it was judged against.
- **Real-time:** NTA developer portal GTFS-R — `TripUpdates` and
  `VehiclePositions` (protobuf, whole-network per call). **Fair usage: 1 call
  per 60 s per token.** Conservative default: alternate the two feeds on a 60 s
  cadence (each feed sampled every 120 s); config flag to tighten to 60 s each
  if the limit proves per-endpoint. All thresholds below assume the 120 s
  worst case.
- **API key:** free registration at developer.nationaltransport.ie — **Alex's
  account task**; key lives only in the VM's environment, never in the repo.

## Outcome taxonomy (the methodology — exactly one class per scheduled trip)

Trip window = scheduled start − 5 min → scheduled end + 15 min (Europe/Dublin;
GTFS times past 24:00:00 handled by service-day semantics — the classic trap).
Rules apply top-to-bottom; the first match wins, which makes classification
exclusive by construction.

| Class | Rule |
|---|---|
| EXCLUDED | poller uptime < 90% of the trip window → excluded from operator stats, counted publicly as tracker downtime |
| CANCELLED | feed marks the trip `CANCELED` at any point in the window |
| COMPLETED | observed, and last observation shows stop-sequence progress ≥ 90% OR is within 10 min of the scheduled final-stop time |
| VANISHED | observed, then no signal for the rest of the window with progress < 75% and > 15 min left — tracked, then gone mid-route |
| UNTRACKED | zero observations in the whole window (uptime ≥ 90%) — the classic ghost. Reported as *untracked*, not "did not run": a dead telematics unit looks identical to a bus that never left the depot, and we say so |

**Headline metric:** ghost rate = (UNTRACKED + VANISHED) / (scheduled − EXCLUDED),
per route, per hour-of-day, per day. Secondary: cancellation rate, and median /
p90 delay at observed stops from TripUpdates. Classification is exclusive and
total — a property test enforces "every trip, exactly one class."

## Architecture — deliberately boring, documented as such

One always-free Oracle Cloud VM (Ubuntu, systemd), three small Python services,
SQLite, static publishing. No Kafka, no Kubernetes — right-sized for ~7,000
trips/day and one node, with the reasoning in the README (boring-tech choice is
part of the story).

```
ghost-bus/
├── ingest/          # poller: GTFS-R fetch loop, raw zstd snapshot archive, heartbeat
├── timetable/       # GTFS zip download, parse, version store, service-day expansion
├── classify/        # observation matcher + outcome classifier (the core)
├── aggregate/       # trip outcomes -> route/hour/day rollups (SQLite -> JSON/CSV)
├── publish/         # static site build + push to gh-pages (site + daily CSVs)
├── site/            # scoreboard templates (self-contained HTML/JS, house style)
├── ops/             # systemd units, provision.sh, healthcheck wiring, runbook
├── tests/           # pytest, no network; synthetic "Fixtureville" GTFS + recorded snapshots
└── docs/superpowers/specs|plans/
```

- **Poller** (60 s loop): fetch → gzip-decode protobuf → filter to in-scope
  agencies → append zstd-compressed snapshot to disk (7-day retention) → write
  observation rows + heartbeat to SQLite. Every poll outcome (ok / HTTP error /
  timeout) is logged; gaps become EXCLUDED windows automatically.
- **Classifier** (runs every 10 min + end-of-service-day finalization):
  expands the active timetable version into the day's scheduled trips, joins
  observations, assigns outcomes for trips whose windows closed. Idempotent —
  reruns never double-count.
- **Publisher** (hourly + nightly): rebuilds aggregate JSON + daily CSVs +
  static site, force-pushes to the `gh-pages` branch via a repo deploy key.
  Site is self-contained (no external requests), house dark style.
- **Monitoring:** healthchecks.io free pinger (poller heartbeat every 5 min →
  email/Telegram on silence); site shows a 30-day tracker-uptime strip. Optional
  later: Jarvis debrief line.

## Site (GitHub Pages)

Route leaderboard (ghost rate, min 30 scheduled trips before a route gets
ranked — no small-sample shaming); route detail (by hour-of-day, worst
stretches); methodology page in plain English incl. the UNTRACKED caveat;
tracker-uptime self-report; daily CSV downloads; "About the data" with
timetable version + snapshot counts. No accounts, no tracking scripts.

## Testing & honesty gates

- pytest, no network. Synthetic **Fixtureville** GTFS (2 routes, ~40 trips,
  incl. a past-midnight trip and a DST boundary day) drives classifier unit
  tests; recorded real snapshots (once the key exists) drive parser tests.
- Property tests: outcome exclusivity/totality; EXCLUDED monotonicity (more
  downtime never *improves* a route's stats); aggregation row-count conservation.
- Publish gate (`run_checks.py`, also in CI): schema validity of published
  JSON/CSVs, aggregate totals reconcile with trip-level outcomes, uptime strip
  matches heartbeat data. The site never publishes numbers the gate didn't pass.
- CI (GitHub Actions, checkout@v5/setup-python@v6): pytest + run_checks on
  fixture data. The VM is production; CI never touches the live feed.

## Phasing & dependencies

1. **P1 — core, no key needed:** timetable module + classifier + aggregates +
   Fixtureville tests + publish gate, fully TDD.
2. **P2 — Alex's account tasks:** NTA API key; Oracle Cloud free-tier VM
   (guided provisioning via ops/provision.sh + runbook).
3. **P3 — live burn-in:** deploy, 3–7 days of data, verify classifier against
   reality (spot-check known routes), tune thresholds via documented amendments.
4. **P4 — public:** scoreboard live after ≥ 2 weeks of baseline data; README
   verdict-style; launch post optional.

**Ongoing ops (accepted):** timetable refresh is automatic; expected manual
touch ≈ minutes/month + acting on healthcheck alerts.

## Risks / honesty notes

- Telematics failures vs true ghosts: UNTRACKED conflates them by necessity — stated
  everywhere the number appears. If TFI's feed has systematic per-operator
  tracking gaps, per-operator UNTRACKED baselines will show it; report, don't
  editorialize.
- Fair-usage ambiguity (per-token vs per-endpoint) → conservative default,
  config to adjust; never risk key revocation.
- The tracker's own outages: self-reported, always.
- No scraping, official API + open data only; NTA licence terms respected
  (attribution on site).
