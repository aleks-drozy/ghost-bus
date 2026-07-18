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

**Headline metric:** ghost rate = (UNTRACKED + VANISHED) / (scheduled − EXCLUDED),
per route, per hour-of-day, per day.

## The tracker grades itself

EXCLUDED exists because a gap in *our* polling looks identical, from the
data, to a gap in a bus's telematics — and it would be dishonest to charge
the operator for our own downtime. So every window where poller uptime drops
below 90% is pulled out of the operator's stats entirely and counted
instead as tracker downtime, in public, on the same site as the bus data.
The scoreboard ships alongside a 30-day tracker-uptime strip, and a publish
gate (`run_checks.py`) refuses to let any aggregate ship if its class counts
don't reconcile with the trip-level outcomes that produced them, if any
ghost rate falls outside [0, 1], or if any outcome isn't one of the five
valid classes. The site never publishes numbers these checks didn't pass.

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
and publish gate are all implemented and covered by the test suite (43
tests, no network, CI-enforced on every push).

**Live deployment: pending two owner tasks** — an NTA developer API key
(free, from `developer.nationaltransport.ie`) and an Oracle Cloud free-tier
VM. Once both exist, `ops/RUNBOOK.md` is the complete, copy-pasteable
provisioning-through-recovery guide for standing the pipeline up for real.
Nothing about the pipeline's logic changes at that point — only that it
starts seeing the live feed instead of fixtures.

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
