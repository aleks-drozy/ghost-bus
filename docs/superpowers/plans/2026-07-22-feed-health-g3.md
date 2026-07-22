# G3 Implementation Plan — feed-health gate + withdrawn days

**Date:** 2026-07-22
**Spec:** `docs/superpowers/specs/2026-07-22-feed-health-design.md`
(ACCEPTED: schedule-relative per-operator gate + drop 2026-07-21).
**Approach:** TDD throughout; detection reuses the existing
`idx_obs_trip` index (no schema change, no new index, no full scans).

## Architecture

```
classify/feedhealth.py   NEW: reporting-fraction detection, pure core + one
                         per-trip indexed query; compute_shields() entry
classify/outcomes.py     OUTCOMES += EXCLUDED_FEED; classify_trip gains
                         feed_degraded intervals; classify_day gains shields
                         + route->agency map; run_classifier wires it
aggregate/rollup.py      _CLASSES += excluded_feed; denominator -= excluded_feed
run_checks.py            conservation sums six classes
publish/dataset.py       DAILY_COLUMNS += excluded_feed; WITHDRAWN_DAYS
                         mechanism (2026-07-21 + reason); manifest section
publish/site.py          COUNT_FIELDS += excluded_feed; trials -= excluded_feed;
                         about-data withdrawn-days section
site/*.tmpl              methodology G3 section; about-data withdrawn days;
                         route-page count row
README.md / RUNBOOK.md   six-class taxonomy; §10 deploy+reclassify
```

## Detection (classify/feedhealth.py)

Constants (methodology — change by commit, not env): `BUCKET_S=600`,
`THRESHOLD=0.5`, `MIN_ACTIVE_TRIPS=30`, `MIN_RUN=2`, `UPTIME_GUARD=0.9`.

`compute_shields(db, trips, agency_of_route) -> dict[agency, list[(start,end)]]`:
1. Denominator: for each trip, active buckets = every 600 s bucket
   overlapping `[start_utc, end_utc]`; count per (agency, bucket).
2. Numerator: per trip, one indexed query for its in-window position
   `ts_utc`s (same index the classifier uses); a trip counts toward a
   bucket it pinged in.
3. Uptime guard: buckets whose heartbeat minute-coverage < 0.9 are not
   evaluated (our downtime is EXCLUDED's job, not the gate's).
4. Degraded: fraction < 0.5 where active >= 30; armed only in runs of >= 2
   consecutive degraded buckets; merged into intervals per agency.

## Classifier effect

After the existing outcome computation: outcome in {VANISHED, UNTRACKED}
AND trip window overlaps a degraded interval for the trip's agency ->
EXCLUDED_FEED. COMPLETED / CANCELLED / EXCLUDED never touched. No shields
(param None/empty, or missing gtfs agency tables) -> byte-identical pre-G3
behaviour.

## Withdrawn days (publish/dataset.py)

`WITHDRAWN_DAYS = {"2026-07-21": "<reason>"}` module constant (auditable by
commit). `complete_service_days` drops them; daily CSVs never written
(prune removes any existing); manifest gains
`withdrawn_days: [{service_date, reason}]`; 14-day baseline counts exclude
them; about-data renders them. Uptime CSVs are NOT withheld — our own
uptime is not an accusation and 07-21's tracker uptime was genuinely fine.

## Test list (RED first unless marked pin)

feedhealth (new file test_feedhealth.py):
1. healthy fractions -> no shields
2. collapse below threshold for >=MIN_RUN buckets -> interval returned,
   correct agency only
3. single degraded bucket (run=1) -> no shield
4. active < MIN_ACTIVE -> bucket never evaluated (overnight noise)
5. tracker-downtime bucket (heartbeats missing) -> not evaluated even if
   fraction low
6. intervals merge across consecutive buckets; disjoint runs stay separate

classifier (test_classifier.py):
7. VANISHED trip overlapping a degraded interval -> EXCLUDED_FEED
8. UNTRACKED trip overlapping -> EXCLUDED_FEED
9. COMPLETED trip overlapping -> stays COMPLETED (shield never credits)
10. CANCELLED overlapping -> stays CANCELLED
11. VANISHED not overlapping -> stays VANISHED
12. no shields -> pre-G3 behaviour (pin, existing suite already proves)
13. EXCLUDED precedence: tracker-down trip stays EXCLUDED, not EXCLUDED_FEED

downstream:
14. rollup: excluded_feed counted; denominator = scheduled - excluded -
    excluded_feed (rates move accordingly)
15. run_checks conservation: six classes sum to scheduled; five-class sum
    now FAILS if excluded_feed present (watch old check fail first)
16. dataset: daily CSV carries excluded_feed column
17. dataset: withdrawn day never in complete_service_days, never gets a
    CSV, existing CSV pruned, not counted toward baseline_required
18. manifest lists withdrawn_days with reasons
19. about-data renders withdrawn day + reason; escapes reason
20. site: trials/judged = scheduled - excluded - excluded_feed
21. methodology page states G3 (pins: "amendment G3", "feed", section text)
