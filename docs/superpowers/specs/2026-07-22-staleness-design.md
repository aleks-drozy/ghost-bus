# Feed Staleness — Amendment Design (proposed G2)

**Date:** 2026-07-22
**Status:** Draft for review — NO implementation, NO classifier change yet
**Depends on:** G1 (`2026-07-19-geo-progress-design.md`), vehicle_ts capture
(merged `1164011`), RUNBOOK §6 burn-in measurements
**Spec amendment:** proposed **G2** (methodology change — dated, public).
This is a real amendment under the project's naming rule: it changes how
trips are classified, unlike the two self-labelled "G2"/"G3" items that were
retracted to schema-addition/tool status.

## Why now

The G1 standing condition said: capture the lag distribution, read it across
a fuller baseline, and only then design a staleness rule. Both halves are now
discharged:

- 2026-07-19 (~20 h of data): distribution first read; 2.3% of pings carried
  positions already older than the classifier's 10-minute COMPLETED window.
- 2026-07-22 (this doc, ~3.3 full days, 1,512,163 position pings): re-read
  confirms the effect is **systematic, stable, and structured** — see below.

Publishing pressure makes the timing matter: the 14-complete-day route gate
matures around **2026-08-01**. If the methodology is going to change, it
should change **before** first publication, with the whole baseline
reclassified under one method — an amendment after publication would need a
public methodology-break note and a dual-method transition period. Deciding
G2 this week is cheap; deciding it in August is not.

## What the fuller baseline says (all read-only, 2026-07-22 ~00:00 UTC)

Pooled §6.1 over the full baseline (1,512,163 position pings, 1,512,161 with
`vehicle_ts` — coverage still effectively 100%):

| min | p50 | p90 | p99 | max |
|----:|----:|----:|----:|----:|
| +1 s | 24 s | 164 s | 829 s | 3,459 s |

1. **The tail is stable day over day** — %>600 s per full day: 2.3 (07-19),
   2.5 (07-20), 2.4 (07-21). Not an outage artifact; this is how the feed
   behaves. (07-18's 8.9% is the overnight-only partial day — see 3.)
2. **No negative clock skew, ever:** min lag is +1 s over 1.5 M pings.
   `vehicle_ts` is monotone-trustworthy on this feed. This is the fact that
   makes Option D (below) available at all.
3. **Staleness is strongly diurnal.** %>600 s by hour (pooled full days):
   ~1.1–1.3% at 05:00–06:00, ~1.6–2.0% through the day, rising after 18:00
   to **5.0% at 22:00 and 13.3% at 23:00**, staying elevated overnight.
   Late-night trips — the ones commuters most fear being ghosted on — are
   where stale credit concentrates.
4. **Staleness differs by operator:** Dublin Bus 3.1% of pings >600 s vs
   Go-Ahead Ireland 1.9%. (Bus Éireann, which we observe but do not grade,
   runs 1.4%.) Any staleness rule therefore redistributes outcomes
   *unevenly* between the two graded operators; the methodology page must
   say so.
5. **Trip-level exposure is far smaller than ping-level exposure.** Of
   25,386 COMPLETED trips (07-19 → 07-21) whose 10-minute time branch fires,
   only **70 (0.28%)** have that near-end credit resting *solely* on pings
   staler than 600 s — i.e. every fresh ping had already fallen silent
   before `end − 10 min`, and only republished stale positions "kept the bus
   alive" into the credit window. Most stale pings arrive mid-route
   surrounded by fresh ones and change nothing. ~23 trips/day is the honest
   upper bound on time-branch flips at a 600 s cutoff.
   - Direction of every one of those errors today: operator-flattering
     (credits COMPLETED on evidence predating the window that justified it).
   - A flip is **not** automatic even for those 70: the residual
     benefit-of-the-doubt rule still returns COMPLETED unless progress
     < 0.75 **and** last (fresh) evidence < `end − 15 min`. 70 is the upper
     bound on exposure, not a predicted VANISHED count.
6. **Caveat that must survive into the amendment text:** we sample each
   endpoint every 120 s, so measured lag below ~120 s is mostly our own
   cadence, not feed staleness. Nothing below fits a threshold under 120 s,
   and p50 = 24 s remains healthy.

Sensitivity of the trip-level exposure to the cutoff (same window-filtered
method as 5; run 07-22, denominator 25,433 time-branch COMPLETED trips —
a few dozen more than in 5 because trips kept classifying between queries):

| cutoff T | trips whose credit rests only on pings staler than T | share |
|---:|---:|---:|
| 180 s | 253 | 1.00% |
| 300 s | 184 | 0.72% |
| 450 s | 119 | 0.47% |
| 600 s | 70 | 0.28% |
| 900 s | 15 | 0.06% |

The curve is smooth — no knee, no natural break. Every candidate T is a
point on a slope, which is itself an argument against defending any single
constant (see Option D).

## The design question

A position ping carries two clocks: `ts_utc` (when we fetched it) and
`vehicle_ts` (when the vehicle says it reported). Today every classifier
input uses `ts_utc`. A republished stale position is therefore
indistinguishable from a live bus in *every* branch:

- **COMPLETED time branch** (`last obs within 10 min of scheduled end`):
  stale republication near the end credits completion. Quantified: 70
  trips / 3 days at a 600 s definition of stale.
- **Geo progress (G1)**: a stale ping still matches a stop and can raise
  progress toward the ≥ 90% branch — crediting arrival at a stop the bus
  left minutes ago.
- **VANISHED early cutoff** (`last obs < end − 15 min`): stale pings push
  `last_ts` later, delaying or suppressing VANISHED.
- **UNTRACKED existence test**: a stale ping still proves a vehicle existed
  *recently* — this one is (correctly) the least affected; see Option C.

## Options considered

### Option A — threshold: stale pings can't satisfy the time branch

Pings with `ts_utc − vehicle_ts > T` are ignored when computing `last_ts`
(time branch and VANISHED cutoff), but still count for existence and geo
progress. Needs a defended constant T. At T = 600 s: exactly the 70-trip
exposure above.

- For: minimal change; T = 600 s has a natural justification (the credit
  window itself is 600 s — evidence older than the window cannot support
  the window).
- Against: any T is a cliff (599 s counts fully, 601 s not at all); the
  measured distribution is smooth, and the constant will need re-defending
  every time someone asks "why 600?".

### Option B — A, plus stale pings can't credit geo progress

Same threshold also filters pings entering `matched_max_seq`. Closes the
G1-raised stake (stale ping credits a stop the bus already left).

- Against, and it is disqualifying as stated: G1's guarantee is that geo
  evidence "can only mask a real ghost, never manufacture a fake one."
  Filtering progress evidence *lowers* progress, which can push a
  genuinely-completed trip below 0.75 and — combined with A — flip it to
  VANISHED on our filtering choice rather than on operator behaviour.
  Progress filtering is only safe under the reinterpretation of Option D.

### Option C — stale pings don't count as observations at all

Would flip live-but-laggy buses to UNTRACKED. **Rejected outright:** it
manufactures ghosts from displaced evidence, violating the project's core
asymmetry (errors may flatter the operator; they must never accuse
falsely). Recorded only so nobody proposes it later.

### Option D — threshold-free: `vehicle_ts` becomes the evidence clock (recommended)

Do not discard anything. Reinterpret: a ping is evidence the bus was at
that position **at `vehicle_ts`**, not at fetch time. Concretely:

- `last_ts` (COMPLETED time branch, VANISHED cutoff) =
  max **`vehicle_ts`** over in-window position pings (fall back to `ts_utc`
  when `vehicle_ts` is NULL — pre-migration rows and any future coverage
  gap, which the data says is ~0%).
- Geo progress start-gate: a ping carries progress only if **`vehicle_ts`**
  ≥ scheduled start (today: `ts_utc` ≥ start). A stale pre-start layover
  position fetched after start no longer earns progress — the honest
  version of B, without discarding evidence: the ping still counts, at the
  time the vehicle itself claims.
- **Existence (UNTRACKED) stays on `ts_utc`** — deliberately asymmetric.
  A window where every fetched ping carries pre-window `vehicle_ts` is
  suspicious, but flipping it to UNTRACKED is an accusation built on our
  reinterpretation of clocks; benefit of the doubt goes to the operator,
  exactly like the residual rule. (Burn-in should count how often this
  pattern occurs; if it is ever nonzero it goes on the methodology page as
  a known flattering case, mirroring the EXCLUDED-uptime honesty note.)

Why this is the strong option:

- **No constant to defend.** The question "how stale is too stale?" answers
  itself: evidence supports exactly the moment the vehicle reported it.
  The 10-minute window then does all the work it was always claimed to do.
- **It needed tonight's two facts to be safe**, and has them: 100%
  `vehicle_ts` coverage (a rule covering a subset of the fleet would grade
  operators by telematics vendor) and min lag +1 s (no negative skew — a
  vehicle clock running ahead of ours would *shrink* lag and could
  over-credit; this feed has none).
- Every error direction remains operator-flattering or neutral; no path
  manufactures a ghost.
- Subsumes A (a stale republished position simply stops moving `last_ts`)
  and achieves B's goal without deleting evidence.

Residual risks, stated honestly:

- **A frozen vehicle clock** (vehicle_ts stuck, position live) would make a
  live bus look silent → VANISHED risk. Mitigation: the burn-in already
  bounds this (p50 24 s, and per-day trend is stable); add a §6 query that
  watches for vehicles whose lag grows linearly with time (the frozen-clock
  signature — indistinguishable from republication in one snapshot, obvious
  over a sequence). If found at material rates, revisit with a hybrid
  (D, but a ping never moves `last_ts` *backwards* below its own fetch-time
  minus some floor). Do not add the hybrid speculatively.
- **DST/timezone integrity of `vehicle_ts`** is feed-side POSIX epoch,
  already normalized to UTC ISO at capture; no local-time parsing anywhere.

## Impact framing (for the methodology page, if adopted)

- Expected headline movement: ≤ ~23 trips/day change class, out of ~8,500
  judged/day — bounded above by the 600 s analysis; the exact number falls
  out of reclassification, and the shift concentrates in late-night hours
  and (asymmetrically) on Dublin Bus, per findings 3–4.
- The change is uniformly in the *stricter* direction: it removes
  operator-flattering credit that rested on republished positions. No trip
  can become COMPLETED under G2 that wasn't already.
- **Reclassify the entire baseline under G2 before first publication** so
  the published series has one methodology from day one. `classify_day` is
  idempotent (`INSERT OR REPLACE`), observations are all retained, and the
  archive can regenerate anything else — a full re-run is cheap (~5 s per
  day per current classifier timing).

## What this doc does NOT do

No code changes, no threshold constants in config, no reclassification.
Implementation is its own TDD plan (`docs/superpowers/plans/`) after Alex
reviews this design. Estimated implementation surface if D is chosen:
`classify/outcomes.py` (last_ts + pre-start gate), `classify/store.py`
(query columns), tests (Fixtureville gains vehicle_ts fixtures; regression
pins: stale-republication trip flips only when fresh evidence is absent;
NULL vehicle_ts reproduces today's behaviour exactly), README methodology +
amendment section, RUNBOOK §6 gains the frozen-clock watch query.

## Decision points for Alex (the ones that shape the feature)

1. **Adopt Option D, or prefer the explicit-threshold Option A?**
   D is recommended; A is defensible if you want the change maximally
   legible to a lay reader ("we ignore positions older than the credit
   window" *is* easier to explain than evidence-clock semantics).
2. **UNTRACKED stays on `ts_utc` (recommended) or moves to `vehicle_ts`?**
   Moving it is the maximally honest reading but lets our reinterpretation
   create accusations; keeping it is the project's usual benefit-of-doubt.
3. **Timing: decide before ~2026-08-01** so the baseline publishes under a
   single methodology, or explicitly accept a dated methodology break.

## Interaction with the 2026-07-21 feed degradation

The same night's analysis found a ~40-minute VehiclePositions partial
outage on 2026-07-21 (~19:20–20:00 UTC) that mass-produced false VANISHED
verdicts — see the companion `2026-07-22-feed-health-design.md` (proposed
G3). It does not distort this doc's numbers materially: an outage *removes*
pings rather than staling them (07-21's stale tail is 2.4%, in line with
the other days), and the affected trips landed in VANISHED, outside the
COMPLETED denominators used here. But the two amendments interlock: G2
tightens what COMPLETED may rest on, G3 protects VANISHED from feed
failures, and both should be decided together before first publication.

## Related

- `2026-07-22-feed-health-design.md` (companion, proposed G3)
- RUNBOOK §6 (measurement queries), README Known limitations (staleness
  entry this design would retire), vault `19-ghost-bus/KNOWN_ISSUES.md`
  (staleness reading 2026-07-19), `DECISIONS.md` ("Capture vehicle_ts, but
  classify nothing on it yet").
