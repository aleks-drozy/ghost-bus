# Publisher & Public Scoreboard — Design

**Date:** 2026-07-19
**Status:** Approved for planning
**Depends on:** Phase 1 core (`2026-07-18-ghost-bus-design.md`), amendment G1
(`2026-07-19-geo-progress-design.md`), live deployment 2026-07-19
**Phase:** P4 (public scoreboard) — the last phase of the original spec

## Problem

The tracker has been live since 2026-07-18 and classifies every scheduled
Dublin Bus / Go-Ahead trip into exactly one honest outcome, but none of it is
visible to anyone. The mission is to *publish* reliability where commuters can
use it. Nothing publishes today: there is no dataset artifact, no site, and no
pipeline from the VM's SQLite database to the public web.

Two things must be true of whatever we build, or it is worse than nothing:

1. **It must not overclaim.** A ranked league table of bus routes is an
   accusation. Every number on it has to be one the data actually supports.
2. **It must be auditable.** Anyone doubting a number needs to be able to
   download the data behind it and recompute.

## Goal

Publish a route-reliability scoreboard and an open dataset, built so that the
published numbers cannot drift from the published data, ranked so that routes
are only called worse than one another when the sample supports it, and gated
so that nothing route-level ships before the agreed baseline exists.

## Decisions taken (with reasoning)

### D1 — Two rates, never summed

`aggregate/rollup.py`'s current `_ghost_rate` computes
`(untracked + vanished) / (scheduled - excluded)`. That single number conflates
two different claims:

- **VANISHED** — a vehicle was observed on the trip and then stopped reporting
  mid-route. Direct evidence of a trip that did not complete as scheduled.
- **UNTRACKED** — no vehicle was ever observed. This is the commuter's ghost,
  but it is *also* what a telematics failure looks like. The project's own
  methodology states UNTRACKED is reported as untracked, never as "did not run".

Publishing them summed would contradict our own methodology page. **The two
rates are computed, published, and displayed separately, and no code path sums
them.** A test pins this.

Note this is a *definition*, not an amendment: nothing has ever been published
under the old combined definition, so no public claim is being revised. It is
recorded in DECISIONS.md as a methodology decision without a G-number.

### D2 — Wilson score intervals, rank by the lower bound

A route with 30 trips and 2 vanished shows 6.7%, but the plausible range spans
roughly 1%–22%. Ranking point estimates makes routes trade places on noise and
invites readers to over-interpret the ordering.

Every published rate carries a 95% Wilson score interval. **The leaderboard
ranks by the lower bound of the VANISHED rate specifically** — descending, worst
first — so a route is placed above another only when the evidence supports the
ordering. The untracked rate is displayed with its own interval in its own
column and never contributes to rank position, because ranking on it would rank
routes by how well their operator's telematics works. Wilson (not the normal approximation) because
observed rates here sit near zero and samples are small, where the naive
interval produces negative bounds and zero-width intervals at 0 successes.

### D3 — The site is built from the published dataset, never from the database

The VM publishes CSVs; GitHub Actions builds HTML *from those CSVs*. The
scoreboard therefore cannot display a number that differs from the downloadable
data, because there is only one source. This is a structural guarantee, stronger
than any consistency check we could write.

### D4 — Split trust: the VM publishes data, CI publishes the site

The VM holds a token scoped to write only the dataset path. It cannot publish
arbitrary HTML. If the box were compromised, an attacker could corrupt numbers
(detectable: the data is public and diffable) but could not inject content into
the page. Site rendering happens in CI where it is reproducible and reviewable.

### D5 — stdlib templating, escaping test-pinned

`string.Template` plus explicit `html.escape()`. No dependency anywhere, matching
the project's documented boring-by-choice architecture; the site is a table, a
set of detail pages, and static prose. Route names come from GTFS — external
input — so escaping is a security requirement, not a nicety, and is pinned by a
test that renders a route named `<script>alert(1)</script>`.

### D6 — Two publication gates, enforced in code

- **≥30 scheduled trips** in the window before a route is ranked (the original
  spec's anti-small-sample-shaming rule). Below-threshold routes still appear,
  under "not enough data yet", with their counts visible.
- **≥14 complete service days** before *any* route-level number is published, in
  the dataset or on the site. Before that the site renders methodology, the
  tracker-uptime strip, and "collecting baseline — day N of 14".

Both are code with tests, not operator discipline. Gating the CSVs on the same
14 days (rather than releasing data from day one) is the faithful reading of the
project's public "no numbers until ≥2 weeks of baseline" commitment; the cost is
losing early build-in-public auditability, accepted deliberately.

### D7 — Complete service days only

A service day is published only when `service_date < today` in Europe/Dublin. A
partial day understates trip counts and distorts every rate computed from it.

### D8 — Rolling 28-day leaderboard window

The leaderboard summarises the last 28 complete service days. Full history stays
in the per-day CSVs. A reliability board should reflect current service, not
average away a fixed problem from months ago.

## Architecture

```
VM (daily, after the classifier):
  run_checks gate ──fail──> publish nothing, exit nonzero, alert
        │ pass
  publish/dataset.py ──> data/daily/YYYY-MM-DD.csv
                         data/uptime/YYYY-MM-DD.csv
                         data/manifest.json
        │ git commit + push (dataset paths only, scoped token)
        ▼
GitHub Actions (on push to data/**):
  publish/site.py ──reads the published CSVs──> _site/ ──> GitHub Pages
```

### New and changed files

| Path | Responsibility |
|---|---|
| `aggregate/rates.py` | **new.** Wilson interval + split rate computation. Pure, no DB, stdlib `math` only. |
| `aggregate/rollup.py` | **modify.** `_ghost_rate` replaced by `vanished_rate` / `untracked_rate` (+ bounds). Existing rollup shape and function names retained. |
| `run_checks.py` | **modify.** `check_rates_bounded` validates both rates; conservation unchanged. |
| `publish/dataset.py` | **new.** DB → CSV + manifest. Runs on the VM. Enforces D6 and D7. |
| `publish/site.py` | **new.** CSV → HTML. Runs in CI. Enforces escaping and the pre-baseline mode. |
| `site/*.html.tmpl`, `site/style.css` | **new.** Templates and one stylesheet. No JS, no external assets. |
| `.github/workflows/publish.yml` | **new.** On `data/**` push: run suite, build site, deploy Pages. |
| `ops/RUNBOOK.md` | **modify.** New section: publishing, the daily timer, token rotation, what to do when the gate fails. |
| `ops/ghostbus-publisher.service/.timer` | **new.** Daily systemd timer for `publish/dataset.py`. |

## Component detail

### `aggregate/rates.py`

```python
def wilson_interval(successes: int, trials: int, z: float = 1.96
                    ) -> tuple[float, float] | None: ...
def rate_with_interval(successes: int, trials: int
                       ) -> tuple[float, float, float] | None: ...
```

Returns `None` when `trials == 0` — an undefined rate is never reported as 0.0.
Bounds are clamped to `[0.0, 1.0]`. Formula (standard Wilson score):

```
p      = k / n
denom  = 1 + z²/n
centre = (p + z²/(2n)) / denom
margin = z * sqrt( p(1-p)/n + z²/(4n²) ) / denom
```

Denominator for both rates is `scheduled - excluded` (tracker downtime never
counts against the operator), consistent with the existing rollup.

### `publish/dataset.py`

`data/daily/YYYY-MM-DD.csv` columns:

```
service_date, route_id, route_short_name, route_long_name, agency_name,
scheduled, excluded, cancelled, completed, vanished, untracked,
vanished_rate, vanished_lo, vanished_hi,
untracked_rate, untracked_lo, untracked_hi
```

`data/uptime/YYYY-MM-DD.csv` columns: `service_date, expected_minutes,
ok_minutes, uptime_fraction`.

`data/manifest.json`:

```json
{
  "schema_version": 1,
  "generated_at": "<UTC ISO-8601>",
  "timetable_hash": "<gtfs_hash from gtfs_meta>",
  "coverage": {"first_day": "YYYY-MM-DD", "last_day": "YYYY-MM-DD",
               "complete_days": 0},
  "scoreboard_ready": false,
  "baseline_required_days": 14,
  "gate": {"conservation": true, "rates_bounded": true, "outcomes_valid": true},
  "counts": {"observations": 0, "snapshots": 0, "trips_classified": 0},
  "unnamed_routes": []
}
```

`unnamed_routes` lists route ids present in outcomes but absent from
`gtfs_routes` — surfaced on the about-data page rather than silently dropped.

Behaviour: refuses to write anything if the publish gate fails (nonzero exit);
writes only complete service days (D7); when `complete_days < 14`, writes the
manifest and the **uptime** CSVs but **no daily route CSVs**.

Uptime is deliberately exempt from the 14-day gate: it is our own downtime, not
a claim about any operator, and publishing it from day one is the
self-accountability the project already promises. The site's pre-baseline mode
depends on it being there.

### `publish/site.py`

Pages generated: `index.html` (leaderboard + uptime strip),
`route/<route_id>.html` per ranked and unranked route, `methodology.html`,
`about-data.html`. Route ids are slugified for filenames (GTFS ids contain
spaces); the slug map is deterministic and recorded in the manifest.

When `scoreboard_ready` is false, `index.html` renders methodology links, the
uptime strip, and the baseline progress line — no route table, no route pages.

**Methodology page must state, in plain English:** the five outcomes; that
UNTRACKED means *we could not see it*, not *it did not run*; that EXCLUDED is
our own downtime and never counts against the operator; the G1 geographic method
and its one-directional error (matching errors can mask a ghost, never fabricate
one); the residual benefit-of-the-doubt rule; that feed staleness is measured but
not yet acted on; and how to read a confidence interval.

**About-data page must state:** timetable hash and load date, coverage dates,
snapshot and observation counts, `unnamed_routes`, schema version, a link to
every CSV, and TFI/NTA attribution for the source feed.

## Testing

- **`aggregate/rates.py`:** Wilson against hand-computed values; `0/30` gives a
  lower bound of exactly 0.0 and a non-zero upper bound; `30/30` gives an upper
  bound of exactly 1.0 and a lower bound below 1.0; `trials=0` returns `None`;
  bounds always within `[0,1]`; the interval narrows monotonically as n grows at
  fixed p.
- **Never-summed invariant:** a test asserting no published row exposes a field
  equal to `vanished + untracked` and that no such combined key exists.
- **`publish/dataset.py`** (Fixtureville + synthetic outcomes): golden CSV
  comparison; gate failure writes nothing and exits nonzero; today's partial day
  is excluded; with 13 complete days nothing route-level is written and
  `scoreboard_ready` is false; at 14 it flips.
- **`publish/site.py`:** golden HTML; **escaping — a route named
  `<script>alert(1)</script>` appears escaped and inert**; a route with 29 trips
  is unranked while 30 is ranked; ranking order follows the Wilson lower bound,
  not the point estimate (pin a case where these disagree); pre-baseline mode
  emits no route table and no route pages; a missing day renders as a gap.
- **CI:** the workflow runs the full suite before building; the build is a smoke
  test in itself (it must not raise on real published data).

## Failure modes

| Condition | Behaviour |
|---|---|
| Publish gate fails | Nothing written; nonzero exit fails the systemd unit, so it surfaces in `systemctl --failed` and the journal; the previously published data stays up rather than being replaced by something unverified |
| Fewer than 14 complete days | Manifest only, `scoreboard_ready:false`, site in baseline mode |
| Route missing from `gtfs_routes` | Falls back to raw route_id, listed in `unnamed_routes` |
| Day with no data | Rendered as a visible gap; never interpolated |
| Route below 30 trips | Listed with counts under "not enough data yet", never ranked |
| `trials == 0` for a rate | Reported as "—", never as 0.0 |

## Security & privacy

- VM token scoped to contents-write on the dataset path only (D4). Rotation
  procedure documented in the runbook.
- Every externally-sourced string escaped before templating (D5).
- No JavaScript, no analytics, no cookies, no external fonts or CDN assets — the
  site makes no third-party requests at all.

## Out of scope

- The staleness amendment (its own spec once the baseline can justify a
  threshold — see KNOWN_ISSUES).
- Per-stop or per-vehicle views; operator-level comparison as a headline claim.
- Historical backfill of the site; the dataset carries history.
- Alerting beyond the existing healthcheck. A failed publish leaves stale data
  published, which is safe; it does not need to page anyone.
