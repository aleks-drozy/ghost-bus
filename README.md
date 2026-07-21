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
| COMPLETED | observed, and the highest stop-sequence progress reached across *all* reports (feed `stop_sequence` merged with our geographic match by taking the maximum, never the last report alone) is ≥ 90% of the trip, OR the last report we have is at or after 10 minutes before the scheduled end — one-sided, so any report after the scheduled end also satisfies this |
| VANISHED | observed, then no signal for the rest of the window with progress < 75% and > 15 min left — tracked, then gone mid-route |
| UNTRACKED | zero vehicle *position* observations in the whole window (uptime ≥ 90%) — the classic ghost. A TripUpdate prediction alone is not proof a vehicle exists, so a trip with TripUpdate rows and no position ping is still untracked. Reported as *untracked*, not "did not run": a dead telematics unit looks identical to a bus that never left the depot, and we say so |

One residual case is decided in the operator's favour: a trip that is
neither clearly completed nor clearly vanished (including any trip last
seen 10-15 minutes before its scheduled end) counts as COMPLETED — benefit
of the doubt.

**Two metrics, never one.** Earlier drafts of this README described a single
"ghost rate" of `(UNTRACKED + VANISHED) / (scheduled − EXCLUDED)`. That number
is not published and no code computes it, because it sums two things that mean
different things: VANISHED is direct evidence a trip stopped mid-route, while
UNTRACKED means *no vehicle was ever seen* — which is the commuter's ghost but
is also exactly what a dead telematics unit looks like. Adding them would
present an unknown as an accusation.

So each route carries a **vanished rate** and an **untracked rate**, reported
separately, each over the same denominator (scheduled − EXCLUDED) and each with
its own confidence interval. No code path sums them, and a test exists whose
only job is to fail if one ever does.

### Spec amendment G1 (2026-07-19): geographic progress

The NTA VehiclePositions feed never populates `current_stop_sequence`
(0/666 vehicles in the 2026-07-18 live probe), so route progress is now
measured geographically: each vehicle GPS ping is matched to the *nearest*
of the trip's own scheduled stops, and counts only if it lies within
`GHOSTBUS_MATCH_RADIUS_M` metres (default 250). Progress is the furthest
matched stop's sequence over the trip's final sequence. Feed-supplied
stop_sequence values, if they ever appear, still count - the two evidence
sources merge by taking the maximum. Only pings at or after the scheduled
start carry progress; pre-start pings (e.g. a vehicle keyed to the trip
during a depot layover) prove existence but cannot complete a trip. Off-route
pings match nothing and contribute nothing.

Geographic evidence can only raise progress; it cannot create a ghost.
Matching errors are therefore one-directional: they can inflate progress and
mask a ghost, never fabricate one. The equidistant tie-break (lower sequence
wins) defends exact duplicate coordinates only, not near-ties - on a loop
route whose outbound and return stops sit close together, a ping drifted
toward the return stop can credit the higher sequence. We do not claim
progress is never over-credited; we claim over-crediting can only mask a
real ghost, never manufacture a fake one.

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
with the trip-level outcomes that produced them, and that neither published
rate falls outside [0, 1]. That is a correctness check on the aggregation
logic, not an independent reconciliation against the raw archived feed
snapshots — and it is weaker than it sounds: the count it reconciles against
is derived from the same rows it is summing, so it confirms the trips we
classified add up, not that every trip in the operator's timetable was
classified in the first place. The site never publishes numbers that gate
didn't pass.

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
- **Feed staleness is measured but not yet acted on.** Every VehiclePositions
  ping now stores the vehicle's own report time (`observations.vehicle_ts`)
  alongside our poll time (`ts_utc`), so a republished stale position is
  finally distinguishable from a live one. **No classifier behaviour depends
  on it yet** — this is deliberate: the 10-minute COMPLETED branch currently
  treats a stale republished position as fresh evidence a bus was moving,
  which is operator-flattering, but choosing a staleness threshold before
  seeing the real lag distribution would just be guessing. Burn-in measures
  the distribution (`ops/RUNBOOK.md` §6); a methodology amendment follows the
  data, not the other way round. There is no amendment number for this: it is
  a schema addition, and the methodology is unchanged since G1. Rows recorded
  before the column existed have `vehicle_ts` NULL and are not evidence of
  freshness either way.
- **Hour-of-day statistics pool across dates** — the route/hour rollup does
  not distinguish, say, "Tuesdays at 5pm" from every day at 5pm ever
  observed.
- **Nearest-stop matching is coarse** (G1): two physically close stops
  (loops, opposite roadsides) can credit the wrong sequence. On ordinary
  routes this is noise around the 75%/90% thresholds; on loop-shaped routes,
  where outbound and return stops sit close together, the near-tie error is
  systematic rather than random, and it is always operator-flattering (it
  can only raise progress). Burn-in must quantify per-route geo-max-sequence
  distributions - loop routes specifically - alongside the overall geo-match
  rate before any number is published.

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
├── publish/           # dataset publisher (VM) and site builder (CI)
├── site/              # HTML templates and the one stylesheet
└── run_checks.py      # the publish gate
```

## The scoreboard & open data

**Nothing here is live yet.** GitHub Pages is not enabled on this repo and
the `ghost-bus-data` repository does not exist. What follows is what this
pipeline will publish, and under what conditions — not a description of a
site you can visit today. `ops/RUNBOOK.md` §8 is the one-time setup
(create the data repo, enable Pages, mint a scoped token) that turns it on.

Once enabled, the scoreboard will be built in CI *from the published CSVs*,
never from the database, so a number on the page could not disagree with
the downloadable data — there would be only one source for both. The
dataset will live in its own repository, written by a credential that can
reach nothing else, so the machine that produces the numbers cannot also
produce the page.

**Site (once enabled):** https://aleks-drozy.github.io/ghost-bus/
**Data (once created):** https://github.com/aleks-drozy/ghost-bus-data

| Path in the data repo | What it will hold |
|---|---|
| `data/daily/` | One CSV per complete service day: per-route counts of every outcome, plus both rates with a Wilson confidence interval |
| `data/uptime/` | One CSV per day of the tracker's own uptime — the downtime that excludes trips from operator stats |
| `data/manifest.json` | Coverage dates, timetable hash and load date, gate results, schema version, and any route ids we could not name |

What the site will and will not say:

- **Two rates, never summed.** VANISHED (a bus was tracked, then
  disappeared mid-route) and UNTRACKED (never seen at all) will be
  published as separate columns. Summing them would assert that every
  untracked trip failed to run, and we cannot know that — a dead
  telematics unit looks identical to a bus that never left the depot.
- **Ranked by the bottom of the confidence interval, not the headline
  rate.** A route will be placed above another only where the sample
  supports the ordering. Ranking uses the vanished rate only; the
  untracked rate never affects position, because ranking on it would rank
  operators by how well their tracking hardware works.
- **Routes with fewer than 30 trips we could judge, in a rolling 28-day
  window, will be listed but never ranked, and no headline rate will be
  claimed for them.** The count that matters is trips we were actually
  watching — scheduled minus excluded — so a route we mostly missed cannot
  be shamed on the handful we saw.
- **Nothing route-level publishes until 14 complete service days exist.**
  Until then the site will show the methodology, the tracker's own
  uptime, and how far into the baseline collection we are. Uptime is exempt
  from that gate and publishes from day one — it is our own failure rate,
  not an accusation about anyone else. If coverage ever falls back below
  the threshold, the route data is withdrawn again.
- **Only complete service days count.** A day in progress understates
  trips and distorts every rate computed from it, so today is never
  published.
- **Gaps are shown as gaps.** A day with no data is never interpolated,
  and a rate with no trips behind it renders as "—", never as zero.

The site will be plain HTML and one stylesheet: no JavaScript, no
analytics, no cookies, no fonts or assets from anywhere else — no
third-party requests at all.

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

Once published, the dataset in `aleks-drozy/ghost-bus-data` will carry the
same attribution: the classifications, rates and intervals in those files
will be ours, not the NTA's. If you use them, link to the methodology so
whoever reads your work can see what the numbers do and do not claim.
