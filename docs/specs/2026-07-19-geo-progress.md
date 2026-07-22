# Geographic Progress + Loader Extensions — Design

**Date:** 2026-07-19
**Status:** Draft for review
**Depends on:** Phase 1 core (spec `2026-07-18-ghost-bus-design.md`), live deployment 2026-07-18
**Spec amendment:** G1 (methodology change — dated, public)

## Problem

The 2026-07-18 live probe proved `current_stop_sequence` is **never populated**
on the NTA VehiclePositions feed (0/666 vehicles). The classifier's `progress`
input is therefore permanently `0.0` on live data:

- COMPLETED can only fire via the time branch (last ping within 10 min of
  scheduled end) or the residual benefit-of-the-doubt rule.
- VANISHED can only fire via the early-cutoff clause.
- The `progress >= 0.90` and `progress < 0.75` branches are dead code on NTA data.

Every vehicle **does** carry GPS `position` (lat/lon, 666/666) — but the poller
does not read it and the `observations` table cannot store it. Additionally,
`trip_outcomes.route_id` holds raw GTFS ids (`"1 G1"`) unusable on a public
scoreboard, and the loader discards `stops.txt` and route names entirely.

## Goal

Revive the progress dimension with **geographic evidence**: match each vehicle
GPS ping to the trip's own scheduled stops; progress = furthest matched stop's
sequence fraction. Also store route display names (unblocks the Phase-2
publisher). Forward-capture only: backfill of the archived `.pb.zst` feeds is a
separate follow-up task.

## Chosen approach (vs alternatives)

**① Nearest-scheduled-stop matching (chosen).** Per ping, find the nearest stop
among that trip's scheduled stops; if within a match radius, credit that stop's
`stop_sequence`. Progress = max credited sequence / trip's max sequence.
Coarse, cheap (no spatial index; one trip's ~40 stops per ping), conservative,
and it plugs into the classifier's existing `progress` variable and
`max_stop_seq` denominator unchanged.

Rejected for v1: **② shapes.txt polyline projection** (heavier math, big loader
footprint on the 1 GB VM, more precision than a coarse ghost detector needs) and
**③ `shape_dist_traveled` hybrid** (depends on NTA populating an optional
column — unverified). Both remain documented upgrades if burn-in shows
nearest-stop is too noisy.

Known limitation, accepted: two physically close stops (loops, opposite
roadsides) can mis-credit a sequence. At the 75%/90% thresholds this is noise,
not systematic bias; burn-in quantifies it.

## Architecture

Three layers change; one pure module is added. No new services, no schema
version table — columns are migrated idempotently at existing init points.

```
timetable/gtfs.py      stops + stop_id + route names into SQLite (migrate at load_gtfs)
ingest/poller.py       read vehicle position.latitude/longitude
classify/store.py      observations gains lat, lon (migrate at init_store)
classify/progress.py   NEW: pure matching functions (haversine, matched_max_seq)
classify/outcomes.py   progress = max(seq evidence, geo evidence) / max_stop_seq
ghostbus_config.py     GHOSTBUS_MATCH_RADIUS_M (default 250.0)
```

### 1. Loader (`timetable/gtfs.py`)

Schema additions:

```sql
CREATE TABLE IF NOT EXISTS gtfs_stops (stop_id TEXT PRIMARY KEY, lat REAL, lon REAL);
-- gtfs_stop_times gains: stop_id TEXT
-- gtfs_routes gains:     route_short_name TEXT, route_long_name TEXT
```

- `stops.txt` loads via the existing `_insert_stream` pattern. Rows whose
  `stop_lat`/`stop_lon` don't parse as floats are **skipped** (an uncodable stop
  simply can't participate in matching; never store a 0,0 "null island").
- `stop_times.txt` insert adds `r["stop_id"]`.
- `routes.txt` insert adds `route_short_name`/`route_long_name` via `.get(...)`
  (optional columns in GTFS; store NULL if absent).
- **Migration:** a small private `_ensure_columns(db, table, {col: decl})`
  helper (PRAGMA table_info + `ALTER TABLE ... ADD COLUMN`) runs inside
  `load_gtfs` before inserts. Legacy tables on the live VM gain the new columns
  on the next `python -m timetable.refresh`; fresh installs get them from
  `_SCHEMA` directly. `load_gtfs` already deletes + reinserts all gtfs_* rows,
  so no data-shape mismatch is possible after migration.

### 2. Store (`classify/store.py`)

- `observations` gains `lat REAL, lon REAL` (NULL for update/cancel kinds, for
  legacy rows, and for any vehicle without a position).
- `init_store` runs the same idempotent column migration (duplicated ~6-line
  private helper rather than a shared import — keeps `timetable` and `classify`
  decoupled).
- `record_observation(..., lat: float | None = None, lon: float | None = None)`.
- `run_poller.main()` already calls `init_store` at startup, so **restarting the
  poller service migrates the live DB** — no manual step.

### 3. Poller (`ingest/poller.py`)

- `parse_feed`: for vehicle entities, when `v.HasField("position")`, emit
  `"lat": v.position.latitude, "lon": v.position.longitude`; otherwise `None`.
  Update/cancel entities always carry `None`.
- `poll_once` passes lat/lon through to `record_observation`. No other flow,
  heartbeat, or archive behavior changes.

### 4. Matching (`classify/progress.py` — new, pure, no DB)

```python
def haversine_m(lat1, lon1, lat2, lon2) -> float: ...

def matched_max_seq(stops: list[tuple[int, float, float]],   # (stop_sequence, lat, lon)
                    pings: list[tuple[float, float]],        # (lat, lon)
                    radius_m: float) -> int | None: ...
```

- For each ping: nearest stop by haversine; credit its sequence **only if the
  distance ≤ radius_m**. A ping matching no stop within the radius contributes
  nothing — an off-route or glitched GPS ping must not fabricate progress.
- Returns the max credited `stop_sequence`, or `None` if nothing matched.
- Ties (equidistant stops) resolve to the **lower** sequence — the conservative
  choice: never over-credit progress toward COMPLETED.
- Cost envelope: ~30 pings/trip × ~40 stops × ~7,000 trips/day ≈ 8–9 M
  haversine calls per daily classifier run — seconds of pure Python on the VM;
  no spatial indexing needed at Dublin scale.

### 5. Classifier (`classify/outcomes.py`)

- Observation query adds `lat, lon`.
- Per trip, load its scheduled stops once:
  `SELECT st.stop_sequence, s.lat, s.lon FROM gtfs_stop_times st JOIN
  gtfs_stops s ON s.stop_id = st.stop_id WHERE st.trip_id=?` (rows with NULL
  `stop_id` — a pre-refresh timetable — simply don't join).
- Evidence merge: `best_seq = max(TripUpdate/VP stop_sequence evidence ∪
  {matched_max_seq(...)})`; `progress = min(1.0, best_seq / max_stop_seq)` as
  today. **Only the progress input changes; taxonomy, precedence, thresholds,
  and the residual operator-benefit rule are untouched.** Geographic evidence
  can only raise progress, never lower any other class's standing.
- `classify_trip` gains `radius_m: float = 250.0`; `run_classifier.main()`
  reads it from config and threads it through `classify_day`.

### 6. Config (`ghostbus_config.py`)

- `GHOSTBUS_MATCH_RADIUS_M`, default `250.0`, via `read_match_radius_m()`.
  Rationale: Dublin stop spacing ~200–400 m, urban GPS accuracy ~10–50 m; the
  radius is a burn-in tunable, not a constant to hard-code.

## Failure & degradation behavior

| Condition | Behavior |
|---|---|
| Timetable not yet refreshed (stop_id NULL) | Geo join yields no stops → progress falls back to exactly today's behavior |
| Vehicle without `position` field | lat/lon NULL → excluded from matching (still proves existence → not UNTRACKED) |
| Stop with unparseable coordinates | Skipped at load; never matchable |
| All pings off-route / outside radius | `matched_max_seq` → None → geo contributes nothing |
| Legacy observation rows (pre-migration) | lat/lon NULL → excluded from matching |

Every degradation path collapses to current (position-existence-only) behavior.
Nothing new can create a false ghost.

## Deployment sequence (runbook addendum)

1. `git pull` on the VM.
2. `python -m timetable.refresh` — migrates gtfs_* schema, loads stops/route
   names from the live TFI zip.
3. `systemctl restart ghostbus-poller` — `init_store` migrates `observations`;
   new pings carry coordinates from this moment.
4. Classifier timer picks everything up on its next run; no unit-file changes.

Order-independent by construction (each step degrades gracefully if the others
haven't happened), but the sequence above starts coordinate capture soonest.

## Testing (TDD; all offline, no network)

- **Fixtureville:** stops currently all share one coordinate (53.3, −6.2).
  Give each stop a distinct coordinate along a line with realistic ~400 m
  spacing so nearest-stop matching is meaningful and radius edges are testable.
- **`test_progress.py` (new):** haversine against a known ground-truth pair;
  match within radius; no-match outside radius; nearest-of-two selection;
  equidistant tie → lower sequence; empty pings/stops; off-route ping ignored.
- **`test_poller.py`:** vehicle entity with position → observation carries
  lat/lon; without position → NULLs; update/cancel entities → NULLs.
- **`test_timetable.py`:** stops loaded with coordinates; stop_id present in
  stop_times; route names stored; bad-coordinate row skipped; **legacy-schema
  DB migrated in place** (build old schema, run load_gtfs, columns appear).
- **`test_store.py`:** observations migration preserves existing rows;
  record_observation round-trips coordinates and defaults to NULL.
- **`test_classifier.py`:** (a) geo-only COMPLETED — pings walk ≥90% of stops,
  last ping deliberately >10 min before scheduled end, so the *progress* branch
  (not the time branch) must fire; (b) geo VANISHED — early pings then silence;
  (c) no-coordinate data reproduces current classification exactly.

## Honesty / documentation obligations

- **Spec amendment G1** recorded in this doc + README methodology section:
  "progress evidence = feed stop_sequence (never populated on NTA) ∪ geographic
  nearest-stop matching of vehicle GPS within GHOSTBUS_MATCH_RADIUS_M."
- README known-limitations: nearest-stop coarseness + the radius tunable.
- Burn-in checklist addition: quantify per-trip geo-match rate (what fraction of
  position pings match a scheduled stop) — a low rate flags radius or matching
  problems before any published number.
- Vault (`19-ghost-bus`) updated after implementation per standing order.

## Out of scope (documented follow-ups)

- **Archive backfill** of the ~1.5 burn-in days already collected (positions
  exist in `state/archive/*.pb.zst`; needs a decoder + reprocessing pass).
- shapes.txt / `shape_dist_traveled` precision upgrades.
- Feed staleness detection (separate KNOWN_ISSUES item).
- Publisher itself (route names merely unblock it here).
