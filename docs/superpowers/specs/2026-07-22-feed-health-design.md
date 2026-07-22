# Feed-Health Exclusion — Amendment Design (proposed G3)

**Date:** 2026-07-22
**Status:** Draft for review — NO implementation, NO classifier change yet
**Depends on:** Phase 1 core spec, G1; companion to
`2026-07-22-staleness-design.md` (same decide-before-publication deadline)
**Spec amendment:** proposed **G3** (methodology change — dated, public)

## The incident that forces the question

2026-07-21, ~19:20–20:00 UTC. VehiclePositions volume collapsed while
TripUpdates ran normally, then recovered fully (all read-only queries,
2026-07-22):

| 10-min bucket | positions 07-21 | same bucket 07-20 |
|---|---:|---:|
| 19:00 | 4,818 | 4,847 |
| 19:20 | 2,556 | 4,404 |
| 19:30 | **1,371** | 4,291 |
| 19:40 | 1,697 | 4,213 |
| 19:50 | 1,669 | 3,278 |
| 20:10 | 2,718 | 3,985 |

Operator split at the trough (19:30 bucket vs 19:00 bucket): Dublin Bus
−79% (2,630 → 557), Bus Éireann −80% (1,534 → 314), Go-Ahead Ireland −19%
(595 → 479). Two operators' AVL reporting failed *together* while a third
barely moved — a shared-upstream telematics/feed event, not buses breaking
down. Our poller was healthy the whole time (heartbeats unbroken), so the
existing EXCLUDED gate — which watches *our* uptime only — saw nothing.

Classifier consequence: **2026-07-21 recorded 344 Dublin Bus VANISHED trips
vs 60 and 116 on the surrounding days, and 280 of the day's 362 VANISHED
verdicts start in the 18:00–19:59 window.** Trips genuinely in motion lost
their position stream mid-route and matched the VANISHED rule exactly.

Why this is the worst possible failure mode for this project: VANISHED is
the accusatory class — the only one that ranks routes. A feed event
mass-produces false accusations, in public, against named operators. The
project's own README says a dead telematics unit must not be reported as
"did not run"; a dead telematics *aggregator* is the same obligation at
fleet scale.

## Principle

Symmetric with EXCLUDED's founding logic. EXCLUDED exists because a gap in
our polling is indistinguishable, trip-by-trip, from a gap in the bus's
telematics — so we refuse to grade what we couldn't see. A collapse in the
*feed's* position volume is the same epistemic situation one level up:
trip-level evidence vanishes for reasons that are visible only in the
aggregate. The honest response is the same: withdraw those windows from
operator grading and say why, publicly.

## Options

### Option A — global position-volume gate

Compute a rolling baseline of position pings per 10-minute bucket (e.g.
trailing 14 days, same bucket-of-week). If a bucket falls below X% of
baseline, mark the interval feed-degraded; any trip whose window overlaps a
degraded interval and whose outcome would be VANISHED or UNTRACKED becomes
EXCLUDED-like instead.

- Against: a global gate would have needed to catch the 07-21 event at
  ~70% loss — but Go-Ahead was healthy that evening. A global threshold
  either misses operator-local outages or, set tight, trips on normal
  variance.

### Option B — per-operator position-volume gate (recommended shape)

Same rolling-baseline comparison, but per graded agency (position pings
join to agency via trip → route, exactly as the burn-in queries do). An
operator whose position volume in a bucket falls below X% of its own
baseline for that bucket-of-week gets its overlapping trips shielded from
the accusatory classes for that interval.

- Catches 07-21 (DB and BÉ collapse independently of GAI's health).
- Grades each operator only against its own reporting norm — no
  cross-operator contamination in either direction.
- Open parameters (deliberately NOT chosen tonight; they need more baseline
  days to fit): baseline length, bucket size, threshold X, and the minimum
  consecutive-bucket run before the gate arms (a single noisy bucket must
  not blank an evening).

### Option C — reclassify to UNTRACKED instead of excluding

Rejected: UNTRACKED's published meaning is "never seen in the window",
which is false for these trips (they were seen, extensively, until the
feed died). Bending a class's meaning to absorb an edge case is how
taxonomies rot.

### Option D — do nothing; note it on the methodology page

Rejected as the steady state, but it IS the correct interim: until G3 is
designed with fitted parameters, any 07-21-style interval discovered in the
baseline must be handled before publication (see below).

## What happens to 2026-07-21 (decision needed regardless of G3)

The baseline now contains a day whose VANISHED count is known-contaminated.
Whatever happens to G3, first publication must not ship 344 accusations of
which ~280 are feed artifacts. Choices:

1. **Exclude 2026-07-21 as a complete service day** (the "gaps are shown as
   gaps" rule already covers missing days honestly) — simplest, loses a
   day of otherwise-good data.
2. Hold publication until G3 exists and reclassifies the interval — cleaner
   series, more work before 2026-08-01.
3. Publish with a dated caveat — weakest; a caveat under a ranking table
   protects nobody.

Recommendation: 1 if G3 slips past the baseline date, 2 if G3 lands in time.

## Detection telemetry to add regardless of adoption (measurement, not amendment)

A RUNBOOK §6-style query — per-agency position pings per 10-minute bucket
vs trailing same-bucket baseline — so feed-degradation events are *seen*
within a day, not discovered three days later by a human noticing a
VANISHED spike. Zero classifier impact; pure ops. This can be built before
any amendment decision.

## Decision points for Alex

1. Adopt the per-operator gate shape (Option B) as the G3 design to be
   parameterized once more baseline exists?
2. 2026-07-21 handling at first publication: drop the day (1) or gate
   publication on G3 (2)?
3. Should the detection telemetry (no methodology change) be built now?
   Recommended yes — it also produces the data that fits B's parameters.

## Related

- `2026-07-22-staleness-design.md` (companion; both change what VANISHED
  can rest on, and both should be decided before the ~2026-08-01 baseline
  maturity)
- README "The tracker grades itself" (EXCLUDED's founding logic — the
  principle this generalizes)
- Vault `19-ghost-bus/KNOWN_ISSUES.md` (2026-07-21 feed-degradation entry)
