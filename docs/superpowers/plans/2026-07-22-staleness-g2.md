# G2 Implementation Plan — vehicle_ts as the evidence clock

**Date:** 2026-07-22
**Spec:** `docs/superpowers/specs/2026-07-22-staleness-design.md`, Option D
(adopted by Alex 2026-07-22). Amendment **G2** — methodology change, dated,
public.
**Approach:** TDD; single small change surface; no schema change, no config
change, no new services.

## The change, in one paragraph

A position ping becomes evidence of the moment the vehicle reported
(`vehicle_ts`), not the moment we fetched it (`ts_utc`). Concretely, inside
`classify_trip` only: the evidence time of a ping is
`min(vehicle_ts, ts_utc)` when `vehicle_ts` is present, else `ts_utc`
(NULL = pre-migration row or feed omission → exactly today's behaviour; the
`min` clamp means a vehicle clock running *ahead* of ours can never extend
credit beyond today's behaviour either). `last_ts` (COMPLETED time branch
and VANISHED cutoff) and the geographic pre-start gate use evidence time.
Window membership and the UNTRACKED existence test stay on `ts_utc` —
deliberately, so the reinterpretation can only remove operator-flattering
credit, never create an accusation.

## Files touched

- `classify/outcomes.py` — query gains `vehicle_ts`; evidence-time helper;
  `last_ts` and geo pre-start gate switch to it; docstrings updated.
- `tests/test_classifier.py` — G2 behaviour tests + pins (below).
- `README.md` — taxonomy wording, amendment G2 section, Known limitations
  (retire the "measured but not acted on" staleness entry; add the
  frozen-vehicle-clock residual risk).
- `ops/RUNBOOK.md` — §5-style G2 upgrade + whole-baseline reclassify
  procedure (deploy is blocked on the P4 owner steps; the runbook must be
  copy-paste ready for that day); §6 note that G2 consumes the measurement.
- Spec doc status → Accepted (Option D).

## Malformed vehicle_ts: crash, don't guess

`ingest/poller.py` pins vehicle_ts to ISO-or-NULL at ingest (the corrupt
uint64 case already degrades to NULL, test-pinned). If `fromisoformat`
raises inside the classifier the database itself is corrupt, and the house
rule from G1 applies: crash loudly rather than silently reshape outcomes.

## Test list (RED first, each watched to fail for the right reason)

New behaviour (must FAIL against today's classifier):
1. `test_g2_stale_republication_cannot_time_complete` — fresh pings to
   minute 15 (progress 0.4), then republished pings near the end whose
   `vehicle_ts` is stuck at minute 15. Today: COMPLETED via time branch.
   G2: VANISHED.
2. `test_g2_stale_prestart_ping_carries_no_progress` — geo ping fetched
   after start at the last stop, `vehicle_ts` 3 min before start (layover
   republication). Today: COMPLETED via geo progress. G2: VANISHED.
3. `test_g2_all_stale_window_is_vanished_not_untracked` — every in-window
   ping is a republication with pre-window `vehicle_ts`. Existence stays on
   `ts_utc` (not UNTRACKED); credit does not (not COMPLETED). G2: VANISHED.

Pins (already pass; they nail what G2 must NOT change):
4. `test_g2_null_vehicle_ts_preserves_behaviour` — NULL vehicle_ts rows
   classify exactly as today (COMPLETED via time branch).
5. `test_g2_fresh_vehicle_ts_time_branch_still_fires` — vehicle_ts ==
   ts_utc near the end → COMPLETED unchanged.
6. `test_g2_future_vehicle_ts_never_extends_credit` — vehicle_ts *after*
   ts_utc (skewed-ahead clock) is clamped to ts_utc → VANISHED, exactly as
   today. Pins the `min()` clamp against a naive `vehicle_ts`-wins
   implementation.
7. `test_g2_residual_benefit_of_doubt_survives` — progress in [0.75, 0.90)
   with stale near-end pings → still COMPLETED via the residual rule. G2
   must not over-flip.

## Out of scope

- VM deploy + whole-baseline reclassification (blocked on P4 owner steps;
  RUNBOOK section written now, executed then).
- G3 feed-health gate (own spec, own cycle).
- Any threshold constant anywhere (that is the point).
