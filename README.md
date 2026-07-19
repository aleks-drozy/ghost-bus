# Ghost Bus Tracker

**Which Dublin buses actually show up? A 24/7 tracker that measures ghost buses — honestly.**

[![tests](https://github.com/aleks-drozy/ghost-bus/actions/workflows/tests.yml/badge.svg)](https://github.com/aleks-drozy/ghost-bus/actions/workflows/tests.yml)

## What this is

A pipeline that polls TFI's GTFS-Realtime feed once a minute, matches every
observation against the published GTFS timetable, and classifies each
scheduled Dublin Bus / Go-Ahead Ireland trip into exactly one outcome — then
publishes a public scoreboard and an open dataset from the result. No
scraping: official API and open data only.

We are publicly grading a state service, so our own measurement has to be
beyond reproach. The methodology below, and the tracker's own uptime
self-report, are not an appendix — they're the point.

## The taxonomy

Every scheduled trip gets exactly one class. Rules apply top-to-bottom —
first match wins, which makes classification exclusive by construction. Trip
window = scheduled start − 5 min → scheduled end + 15 min.

| Class | Rule |
|---|---|
| EXCLUDED | poller uptime < 90% of the trip window → excluded from operator stats, counted publicly as tracker downtime |
| CANCELLED | feed marks the trip `CANCELED` at any point in the window |
| COMPLETED | observed, and last observation shows stop-sequence progress ≥ 90% OR is within 10 min of the scheduled final-stop time |
| VANISHED | observed, then no signal for the rest of the window with progress < 75% and > 15 min left — tracked, then gone mid-route |
| UNTRACKED | zero observations in the whole window (uptime ≥ 90%) — the classic ghost. Reported as *untracked*, not "did not run": a dead telematics unit looks identical to a bus that never left the depot, and we say so |

One residual case is decided in the operator's favour: a trip that is
neither clearly completed nor clearly vanished (including any trip last
seen 10-15 minutes before its scheduled end) counts as COMPLETED — benefit
of the doubt.

**Headline metric:** ghost rate = (UNTRACKED + VANISHED) / (scheduled − EXCLUDED),
per route, per hour-of-day, per day.

### Spec amendment G1 (2026-07-19): geographic progress

The NTA VehiclePositions feed never populates `current_stop_sequence`
(0/666 vehicles in the 2026-07-18 live probe), so route progress is now
measured geographically: each vehicle GPS ping is matched to the *nearest*
of the trip's own scheduled stops, and counts only if it lies within
`GHOSTBUS_MATCH_RADIUS_M` metres (default 250). Progress is the furthest
matched stop's sequence over the trip's final sequence. Feed-supplied
stop_sequence values, if they ever appear, still count - the two evidence
sources merge by taking the maximum. Geographic evidence can only raise
progress; it cannot create a ghost. Off-route pings match nothing and
contribute nothing, and equidistant matches credit the lower sequence -
progress is never over-credited.

## The tracker grades itself

EXCLUDED exists because a gap in *our* polling looks identical, from the
data, to a gap in a bus's telematics — and it would be dishonest to charge
the operator for our own downtime. So every window where poller uptime drops
below 90% is pulled out of the operator's stats entirely and counted
instead as tracker downtime, in public, on the same site as the bus data.
The scoreboard ships alongside a 30-day tracker-uptime strip, gated by
`run_checks.py`. Today that gate validates the outcome vocabulary (every
outcome is one of the five valid classes) and the internal consistency of
the rollup code path itself — that its own aggregate class counts reconcile
with the trip-level outcomes that produced them, and that no ghost rate
falls outside [0, 1]. That is a correctness check on the aggregation logic,
not yet an independent reconciliation against the raw archived feed
snapshots; that independent artifact reconciliation lands with the
publisher (Phase 2). The site never publishes numbers today's gate didn't
pass.

## Quick start

```bash
git clone https://github.com/aleks-drozy/ghost-bus.git
cd ghost-bus
python -m venv .venv
.venv/Scripts/activate       # Windows; use .venv/bin/activate on macOS/Linux
pip install -r requirements-dev.txt
python -m pytest
```

Tests never touch the network — the classifier, aggregates, and publish
gate are all exercised against a synthetic "Fixtureville" GTFS network (2
routes, ~40 trips, including a past-midnight trip and a DST-boundary day),
plus real GTFS-Realtime protobufs built in-process.

## Status

**Core pipeline: complete and tested.** The timetable engine, five-class
classifier, route/day and route/hour aggregates, offline-testable poller,
and publish gate are all implemented and covered by the test suite (47
tests, no network, runs in CI on every push once the repo is published).

**Live deployment: pending two owner tasks** — an NTA developer API key
(free, from `developer.nationaltransport.ie`) and an Oracle Cloud free-tier
VM. Once both exist, `ops/RUNBOOK.md` is the complete, copy-pasteable
provisioning-through-recovery guide for standing the pipeline up for real.
Nothing about the pipeline's logic changes at that point — only that it
starts seeing the live feed instead of fixtures.

## Known limitations (v1 core, before live burn-in)

- **VehiclePositions coverage on the live NTA feed is unverified.** If
  vehicles are sparsely reported, UNTRACKED will overcount — burn-in
  (Phase 3) must measure this before any number is published.
- **Advance cancellations that leave the feed before a trip's window opens
  would classify UNTRACKED**, not CANCELLED — feed retention behaviour
  around cancellations is to be verified in burn-in.
- **Feed staleness is not yet detected** — a frozen upstream feed would look
  like healthy polling. A staleness check is planned for Phase 2.
- **Hour-of-day statistics pool across dates** — the route/hour rollup does
  not distinguish, say, "Tuesdays at 5pm" from every day at 5pm ever
  observed.
- **Nearest-stop matching is coarse** (G1): two physically close stops
  (loops, opposite roadsides) can credit the wrong sequence. At the
  75%/90% thresholds this is noise rather than systematic bias; the
  burn-in quantifies the geo-match rate before any number is published.

## Architecture

Deliberately boring, on purpose: one always-free Oracle Cloud VM (Ubuntu,
systemd), three small Python services, SQLite, static publishing. No Kafka,
no Kubernetes — right-sized for ~7,000 trips/day on one node.

```
ghost-bus/
├── ingest/          # poller: GTFS-R fetch loop, zstd snapshot archive, heartbeat
├── timetable/        # GTFS zip download, parse, version store, service-day expansion
├── classify/         # observation matcher + outcome classifier (the core)
├── aggregate/         # trip outcomes -> route/hour/day rollups
├── ops/               # systemd units, RUNBOOK
├── tests/             # pytest, no network; synthetic "Fixtureville" GTFS
└── run_checks.py      # the publish gate
```

## Docs

- [Design spec](docs/superpowers/specs/2026-07-18-ghost-bus-design.md) — the
  full methodology, scope, risks, and honesty notes; normative for the
  taxonomy above.
- [Core implementation plan](docs/superpowers/plans/2026-07-18-ghost-bus-core.md) —
  the task-by-task TDD plan that built the pipeline in this repo.
- [Ops runbook](ops/RUNBOOK.md) — provisioning, install, health checks, and
  recovery procedures for the production VM.

## Data & attribution

Ghost Bus Tracker is **not affiliated with TFI or the NTA**; data ©
National Transport Authority, used under its developer terms with
attribution. Static timetables come from the "Operator GTFS Schedule
files" on data.gov.ie; real-time data comes from the NTA's GTFS-Realtime
developer API (`TripUpdates` and `VehiclePositions`).
