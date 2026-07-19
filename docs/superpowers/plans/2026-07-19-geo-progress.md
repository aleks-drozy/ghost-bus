# Geographic Progress + Loader Extensions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Revive the classifier's dead `progress` input by matching vehicle GPS pings to each trip's scheduled stops, and store route display names for the future publisher.

**Architecture:** Three existing layers extend (GTFS loader stores stops/stop_id/route names; poller captures `position.latitude/longitude`; observations table gains lat/lon) and one new pure module (`classify/progress.py`) does nearest-stop matching. The classifier merges geographic evidence with feed stop_sequence evidence by taking the max. Every degradation path (un-refreshed timetable, GPS-less vehicle, off-route ping, legacy rows) collapses to current behavior.

**Tech Stack:** Python 3.12 stdlib only for new code (`math`, `sqlite3`); pytest, offline; existing deps unchanged.

**Spec:** `docs/superpowers/specs/2026-07-19-geo-progress-design.md` (amendment G1).

## Global Constraints

- No new dependencies. New module uses stdlib `math` only.
- All tests offline — no network, no live feed data.
- Honesty invariants: geographic evidence may only RAISE progress; a ping matching no stop within the radius contributes nothing; equidistant ties credit the LOWER stop_sequence; taxonomy/precedence/thresholds in `classify_trip` are otherwise untouched.
- Schema migrations are idempotent `ALTER TABLE ... ADD COLUMN` guarded by `PRAGMA table_info`; they run at existing init points (`load_gtfs`, `init_store`) — no schema-version table, no manual migration step.
- GTFS-RT protobuf coordinates are 32-bit floats: tests MUST compare with `pytest.approx(..., abs=1e-4)`, never exact equality.
- Default match radius 250.0 m; env override `GHOSTBUS_MATCH_RADIUS_M`.
- Run the full suite (`python -m pytest -q`) before every commit; every test must pass.
- Commit messages: conventional commits, ending with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Loader — gtfs_stops, stop_id in stop_times, route names, migration

**Files:**
- Modify: `timetable/gtfs.py` (schema lines 20-32, `load_gtfs` lines 68-101)
- Modify: `tests/fixtureville.py` (stops list lines 36-37)
- Test: `tests/test_timetable.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: tables `gtfs_stops(stop_id TEXT PRIMARY KEY, lat REAL, lon REAL)`; `gtfs_stop_times` with 4th column `stop_id TEXT`; `gtfs_routes` with `route_short_name TEXT, route_long_name TEXT`. Task 5 joins `gtfs_stop_times.stop_id = gtfs_stops.stop_id`.

- [ ] **Step 1: Give Fixtureville stops distinct coordinates**

In `tests/fixtureville.py`, replace the `stops = [...]` line (currently every stop at 53.3/-6.2) with a coordinate table plus one uncodable stop:

```python
# Distinct coordinates ~400 m apart along a north-south line so nearest-stop
# matching is meaningful in tests (0.0036 deg latitude ~= 400.3 m).
_STOP_COORDS = {
    "S1": ("53.3000", "-6.2000"), "S2": ("53.3036", "-6.2000"),
    "S3": ("53.3072", "-6.2000"), "S4": ("53.3108", "-6.2000"),
    "S5": ("53.3144", "-6.2000"), "S6": ("53.3180", "-6.2000"),
    "S7": ("53.3216", "-6.2000"),
}
```

and inside `build_gtfs_zip` replace the `stops = [...]` list comprehension with:

```python
    stops = [{"stop_id": s, "stop_name": f"Stop {s}",
              "stop_lat": _STOP_COORDS[s][0], "stop_lon": _STOP_COORDS[s][1]}
             for s in sorted(set(_STOPS_R1 + _STOPS_R2))]
    # An uncodable stop: the loader must skip it, not store garbage coordinates.
    stops.append({"stop_id": "SBAD", "stop_name": "Stop SBAD",
                  "stop_lat": "", "stop_lon": ""})
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_timetable.py`:

```python
def test_load_stores_stop_coordinates_and_skips_uncodable(db):
    stops = {sid: (lat, lon) for sid, lat, lon
             in db.execute("SELECT stop_id, lat, lon FROM gtfs_stops")}
    assert stops["S1"] == (pytest.approx(53.3000), pytest.approx(-6.2000))
    assert stops["S3"] == (pytest.approx(53.3072), pytest.approx(-6.2000))
    assert "SBAD" not in stops  # blank coordinates -> skipped, never stored
    assert len(stops) == 7


def test_load_stores_stop_id_in_stop_times(db):
    rows = db.execute(
        "SELECT stop_sequence, stop_id FROM gtfs_stop_times "
        "WHERE trip_id='R1_wk_00' ORDER BY stop_sequence").fetchall()
    assert rows == [(1, "S1"), (2, "S2"), (3, "S3"), (4, "S4"), (5, "S5")]


def test_load_stores_route_names(db):
    rows = dict((rid, (s, l)) for rid, s, l in db.execute(
        "SELECT route_id, route_short_name, route_long_name FROM gtfs_routes"))
    assert rows["R1"] == ("1", "Fixtureville Main")
    assert rows["R3"] == ("3", "Fixtureville Crosstown")


def test_load_migrates_legacy_schema(tmp_path):
    # A DB created by the pre-G1 loader: no stop_id column, no name columns,
    # no gtfs_stops table. load_gtfs must migrate it in place.
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE gtfs_stop_times (trip_id TEXT, stop_sequence INTEGER, dep_seconds INTEGER);"
        "CREATE TABLE gtfs_routes (route_id TEXT PRIMARY KEY, agency_id TEXT);")
    zip_path = tmp_path / "f.zip"
    build_gtfs_zip(zip_path)
    load_gtfs(zip_path, conn)
    (sid,) = conn.execute("SELECT stop_id FROM gtfs_stop_times "
                          "WHERE trip_id='R1_wk_00' AND stop_sequence=1").fetchone()
    assert sid == "S1"
    (short,) = conn.execute(
        "SELECT route_short_name FROM gtfs_routes WHERE route_id='R1'").fetchone()
    assert short == "1"
    (n,) = conn.execute("SELECT COUNT(*) FROM gtfs_stops").fetchone()
    assert n == 7
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_timetable.py -q`
Expected: the 4 new tests FAIL (`no such table: gtfs_stops`, missing columns); all pre-existing tests still PASS.

- [ ] **Step 4: Implement the loader changes**

In `timetable/gtfs.py`:

(a) Update `_SCHEMA` — replace the `gtfs_stop_times` and `gtfs_routes` lines and add `gtfs_stops`:

```python
_SCHEMA = """
CREATE TABLE IF NOT EXISTS gtfs_trips (trip_id TEXT PRIMARY KEY, route_id TEXT, service_id TEXT);
CREATE TABLE IF NOT EXISTS gtfs_stop_times (trip_id TEXT, stop_sequence INTEGER, dep_seconds INTEGER, stop_id TEXT);
CREATE TABLE IF NOT EXISTS gtfs_calendar (
  service_id TEXT PRIMARY KEY, monday INT, tuesday INT, wednesday INT, thursday INT,
  friday INT, saturday INT, sunday INT, start_date TEXT, end_date TEXT);
CREATE TABLE IF NOT EXISTS gtfs_calendar_dates (
  service_id TEXT, date TEXT, exception_type INTEGER);
CREATE TABLE IF NOT EXISTS gtfs_routes (route_id TEXT PRIMARY KEY, agency_id TEXT,
  route_short_name TEXT, route_long_name TEXT);
CREATE TABLE IF NOT EXISTS gtfs_agency (agency_id TEXT PRIMARY KEY, agency_name TEXT);
CREATE TABLE IF NOT EXISTS gtfs_stops (stop_id TEXT PRIMARY KEY, lat REAL, lon REAL);
CREATE TABLE IF NOT EXISTS gtfs_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_stop_times_trip ON gtfs_stop_times(trip_id);
"""
```

(b) Add the migration helper and a stops-row filter after `_insert_stream`:

```python
def _ensure_columns(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """Idempotently add columns that pre-G1 databases lack (live VM migration)."""
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _stop_rows(rows):
    for r in rows:
        try:
            yield (r["stop_id"], float(r["stop_lat"]), float(r["stop_lon"]))
        except (KeyError, ValueError):
            continue  # uncodable stop: it simply can't participate in geo matching
```

(c) In `load_gtfs`, after `db.executescript(_SCHEMA)` add:

```python
    _ensure_columns(db, "gtfs_stop_times", {"stop_id": "TEXT"})
    _ensure_columns(db, "gtfs_routes",
                    {"route_short_name": "TEXT", "route_long_name": "TEXT"})
```

(d) Add `db.execute("DELETE FROM gtfs_stops")` alongside the other DELETEs.

(e) Update the routes insert (explicit columns, `or None` so blank CSV cells store NULL):

```python
        _insert_stream(db, "INSERT INTO gtfs_routes "
                       "(route_id, agency_id, route_short_name, route_long_name) "
                       "VALUES (?,?,?,?)",
                       ((r["route_id"], r["agency_id"],
                         r.get("route_short_name") or None,
                         r.get("route_long_name") or None) for r in rows("routes.txt")))
```

(f) Update the stop_times insert (explicit columns):

```python
        _insert_stream(db, "INSERT INTO gtfs_stop_times "
                       "(trip_id, stop_sequence, dep_seconds, stop_id) VALUES (?,?,?,?)",
                       ((r["trip_id"], int(r["stop_sequence"]),
                         gtfs_seconds(r["departure_time"]), r["stop_id"])
                        for r in rows("stop_times.txt")))
```

(g) Add the stops insert after the agency insert (`INSERT OR REPLACE` guards duplicate stop_ids in the national feed):

```python
        _insert_stream(db, "INSERT OR REPLACE INTO gtfs_stops VALUES (?,?,?)",
                       _stop_rows(rows("stops.txt")))
```

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: all tests PASS (47 existing + 4 new).

- [ ] **Step 6: Commit**

```bash
git add timetable/gtfs.py tests/fixtureville.py tests/test_timetable.py
git commit -m "feat(timetable): store stops, stop_id and route names; migrate legacy schema (G1)"
```

---

### Task 2: Store — observations lat/lon + migration

**Files:**
- Modify: `classify/store.py` (schema lines 8-13, `init_store` lines 16-18, `record_observation` lines 26-31)
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `observations` gains `lat REAL, lon REAL`; `record_observation(db, trip_id, service_date, ts_utc, kind, stop_sequence=None, lat=None, lon=None)`. Tasks 3 and 5 rely on these exact names.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_store.py`:

```python
def test_observation_roundtrip_with_coordinates(db):
    record_observation(db, "R1_wk_00", "2026-03-23", T0.isoformat(), "position",
                       None, 53.3036, -6.2)
    rows = db.execute("SELECT stop_sequence, lat, lon FROM observations").fetchall()
    assert rows == [(None, pytest.approx(53.3036), pytest.approx(-6.2))]


def test_init_store_migrates_legacy_observations():
    # A DB written by the pre-G1 poller: no lat/lon columns, existing rows.
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE observations (trip_id TEXT, service_date TEXT, "
                 "ts_utc TEXT, kind TEXT, stop_sequence INTEGER)")
    conn.execute("INSERT INTO observations VALUES "
                 "('T1','2026-03-23','2026-03-23T07:00:00+00:00','position',2)")
    init_store(conn)
    row = conn.execute(
        "SELECT trip_id, stop_sequence, lat, lon FROM observations").fetchone()
    assert row == ("T1", 2, None, None)  # legacy row intact, coords NULL
    record_observation(conn, "T2", "2026-03-23", T0.isoformat(), "position",
                       1, 53.3, -6.2)  # new writes work post-migration
    (n,) = conn.execute("SELECT COUNT(*) FROM observations").fetchone()
    assert n == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_store.py -q`
Expected: the 2 new tests FAIL (`no such column: lat` / TypeError on extra args); existing tests PASS.

- [ ] **Step 3: Implement store changes**

In `classify/store.py`:

(a) Update `_SCHEMA`'s observations line:

```python
_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
  trip_id TEXT, service_date TEXT, ts_utc TEXT, kind TEXT, stop_sequence INTEGER,
  lat REAL, lon REAL);
CREATE INDEX IF NOT EXISTS idx_obs_trip ON observations(trip_id, service_date);
CREATE TABLE IF NOT EXISTS heartbeats (ts_utc TEXT PRIMARY KEY, ok INTEGER);
"""
```

(b) Add the migration helper (deliberately duplicated from `timetable/gtfs.py` rather than imported — keeps the two layers decoupled):

```python
def _ensure_columns(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """Idempotently add columns that pre-G1 databases lack (live VM migration)."""
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
```

(c) Update `init_store`:

```python
def init_store(db: sqlite3.Connection) -> None:
    db.executescript(_SCHEMA)
    _ensure_columns(db, "observations", {"lat": "REAL", "lon": "REAL"})
    db.commit()
```

(d) Update `record_observation` (explicit column list — robust to column order on migrated tables):

```python
def record_observation(db: sqlite3.Connection, trip_id: str, service_date: str,
                       ts_utc: str, kind: str, stop_sequence: int | None = None,
                       lat: float | None = None, lon: float | None = None) -> None:
    if kind not in ("position", "update", "cancel"):
        raise ValueError(f"unknown observation kind {kind!r}")
    db.execute("INSERT INTO observations "
               "(trip_id, service_date, ts_utc, kind, stop_sequence, lat, lon) "
               "VALUES (?,?,?,?,?,?,?)",
               (trip_id, service_date, ts_utc, kind, stop_sequence, lat, lon))
    db.commit()
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add classify/store.py tests/test_store.py
git commit -m "feat(store): observations carry vehicle coordinates; migrate legacy DB (G1)"
```

---

### Task 3: Poller — capture vehicle GPS

**Files:**
- Modify: `ingest/poller.py` (`parse_feed` lines 24-50, `poll_once` observation loop lines 79-88)
- Test: `tests/test_poller.py`

**Interfaces:**
- Consumes: Task 2's `record_observation(..., lat=None, lon=None)`.
- Produces: `parse_feed` dicts always carry `"lat"` and `"lon"` keys (float or None). Positions from live NTA data now land in the DB with coordinates.

- [ ] **Step 1: Extend the vehicle test helper and write failing tests**

In `tests/test_poller.py`, replace the `vehicle` helper:

```python
def vehicle(trip_id, seq, start_date="20260323", lat=None, lon=None):
    e = rt.FeedEntity()
    e.id = f"v-{trip_id}"
    e.vehicle.trip.trip_id = trip_id
    e.vehicle.trip.start_date = start_date
    e.vehicle.current_stop_sequence = seq
    if lat is not None:
        e.vehicle.position.latitude = lat
        e.vehicle.position.longitude = lon
    return e
```

Append new tests (protobuf coordinates are 32-bit floats — always `approx`):

```python
def test_parse_vehicle_position_coordinates():
    raw = make_feed([vehicle("C", 2, lat=53.3492, lon=-6.2603)])
    (o,) = parse_feed(raw)
    assert o["lat"] == pytest.approx(53.3492, abs=1e-4)
    assert o["lon"] == pytest.approx(-6.2603, abs=1e-4)


def test_parse_vehicle_without_position_gives_none():
    raw = make_feed([vehicle("C", 2)])
    (o,) = parse_feed(raw)
    assert o["lat"] is None and o["lon"] is None


def test_parse_updates_and_cancels_carry_no_coordinates():
    raw = make_feed([trip_update("A", max_seq=4), trip_update("B", cancelled=True)])
    for o in parse_feed(raw):
        assert o["lat"] is None and o["lon"] is None


def test_poll_once_stores_coordinates(tmp_path):
    db = sqlite3.connect(":memory:")
    init_store(db)
    raw = make_feed([vehicle("C", 2, lat=53.3492, lon=-6.2603)])
    now = dt.datetime(2026, 3, 23, 7, 0, tzinfo=UTC)
    poll_once(db, fetch_fn=lambda: raw, now_fn=lambda: now,
              route_filter=None, archive_dir=None)
    (lat, lon) = db.execute("SELECT lat, lon FROM observations").fetchone()
    assert lat == pytest.approx(53.3492, abs=1e-4)
    assert lon == pytest.approx(-6.2603, abs=1e-4)
```

Add `import pytest` to the imports if not present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_poller.py -q`
Expected: 4 new tests FAIL (KeyError `'lat'`); existing tests PASS.

- [ ] **Step 3: Implement parse + passthrough**

In `ingest/poller.py`, `parse_feed`: add `"lat": None, "lon": None` to both trip_update dicts (cancel and update), and replace the vehicle branch:

```python
        elif entity.HasField("vehicle"):
            v = entity.vehicle
            has_pos = v.HasField("position")
            out.append({"trip_id": v.trip.trip_id, "kind": "position",
                        "stop_sequence": v.current_stop_sequence if v.HasField("current_stop_sequence") else None,
                        "start_date": v.trip.start_date,
                        "lat": v.position.latitude if has_pos else None,
                        "lon": v.position.longitude if has_pos else None})
```

In `poll_once`, extend the `record_observation` call:

```python
        record_observation(db, obs["trip_id"], _service_date(obs["start_date"]),
                           now.isoformat(), obs["kind"], obs["stop_sequence"],
                           obs["lat"], obs["lon"])
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add ingest/poller.py tests/test_poller.py
git commit -m "feat(ingest): capture vehicle GPS coordinates from VehiclePositions (G1)"
```

---

### Task 4: `classify/progress.py` — pure nearest-stop matching

> **Execution note (session owner):** `matched_max_seq` is the methodological heart of amendment G1 and has been offered to Alex to implement personally. Pause before Step 3's implementation of `matched_max_seq` and check; scaffold, `haversine_m`, and tests proceed normally either way.

**Files:**
- Create: `classify/progress.py`
- Create: `tests/test_progress.py`

**Interfaces:**
- Consumes: nothing — pure functions, no DB, stdlib `math` only.
- Produces (Task 5 relies on these exact signatures):
  - `haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float`
  - `matched_max_seq(stops: list[tuple[int, float, float]], pings: list[tuple[float, float]], radius_m: float) -> int | None` — stops are `(stop_sequence, lat, lon)`, pings are `(lat, lon)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_progress.py`. Geometry note: the fixture stops sit 400.3 m apart on a meridian, so a ping between two stops splits that 400 m; 250 m radius means both stops can be in range near the middle.

```python
import pytest

from classify.progress import haversine_m, matched_max_seq

# 5 stops on a meridian, 400.3 m apart (mirrors Fixtureville geometry).
STOPS = [(i + 1, 53.3000 + 0.0036 * i, -6.2000) for i in range(5)]
RADIUS = 250.0


def test_haversine_known_distance():
    # 0.0036 deg latitude on a meridian = 0.0036/180*pi*6371000 = 400.3 m
    d = haversine_m(53.3000, -6.2000, 53.3036, -6.2000)
    assert d == pytest.approx(400.3, abs=0.5)


def test_ping_at_stop_matches_it():
    assert matched_max_seq(STOPS, [(53.3001, -6.2000)], RADIUS) == 1  # ~11 m from S1


def test_ping_far_from_route_matches_nothing():
    assert matched_max_seq(STOPS, [(53.5000, -6.2000)], RADIUS) is None  # ~22 km


def test_nearest_stop_wins_not_highest_sequence():
    # 177.9 m from stop 1, 222.4 m from stop 2 - BOTH within 250 m. Correct
    # nearest-stop matching credits 1; sloppy max-seq-in-radius would say 2.
    assert matched_max_seq(STOPS, [(53.3016, -6.2000)], RADIUS) == 1


def test_equidistant_tie_credits_lower_sequence():
    # Exact midpoint between stops 1 and 2: never over-credit progress.
    assert matched_max_seq(STOPS, [(53.3018, -6.2000)], RADIUS) == 1


def test_max_over_pings_walks_the_route():
    pings = [(53.3000 + 0.0036 * i, -6.2000) for i in range(5)]
    assert matched_max_seq(STOPS, pings, RADIUS) == 5


def test_off_route_ping_does_not_poison_good_pings():
    pings = [(53.3072, -6.2000), (53.9000, -6.9000)]  # at stop 3 + garbage
    assert matched_max_seq(STOPS, pings, RADIUS) == 3


def test_empty_inputs_give_none():
    assert matched_max_seq([], [(53.3, -6.2)], RADIUS) is None
    assert matched_max_seq(STOPS, [], RADIUS) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_progress.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'classify.progress'`.

- [ ] **Step 3: Implement the module**

Create `classify/progress.py`:

```python
"""Geographic progress: match vehicle GPS pings to a trip's scheduled stops.

Pure functions, no DB - the classifier queries SQLite and passes plain tuples.
A ping credits its NEAREST scheduled stop, and only if that stop lies within
radius_m; anything further contributes nothing (an off-route or glitched GPS
fix must never fabricate progress). Equidistant ties credit the lower
stop_sequence - progress is never over-credited.
"""
from __future__ import annotations

import math

_EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def matched_max_seq(stops: list[tuple[int, float, float]],
                    pings: list[tuple[float, float]],
                    radius_m: float) -> int | None:
    best: int | None = None
    for plat, plon in pings:
        near_seq: int | None = None
        near_d: float | None = None
        for seq, slat, slon in stops:
            d = haversine_m(plat, plon, slat, slon)
            if near_d is None or d < near_d or (d == near_d and seq < near_seq):
                near_seq, near_d = seq, d
        if near_d is not None and near_d <= radius_m:
            if best is None or near_seq > best:
                best = near_seq
    return best
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add classify/progress.py tests/test_progress.py
git commit -m "feat(classify): pure nearest-stop geographic matching (G1)"
```

---

### Task 5: Classifier merge + radius config + entry-point threading

**Files:**
- Modify: `classify/outcomes.py` (module docstring, `classify_trip` lines 28-53, `classify_day` lines 56-69)
- Modify: `ghostbus_config.py`
- Modify: `classify/run_classifier.py` (`run_for_dates` lines 24-36, `main` lines 39-48)
- Test: `tests/test_classifier.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: Task 4's `matched_max_seq`; Task 1's `gtfs_stop_times.stop_id`/`gtfs_stops`; Task 2's observation lat/lon columns.
- Produces: `classify_trip(db, trip, radius_m=250.0)`, `classify_day(db, trips, now_utc, radius_m=250.0)`, `run_for_dates(db, dates, agency_names, now_utc, radius_m=250.0)`, `ghostbus_config.read_match_radius_m() -> float` (env `GHOSTBUS_MATCH_RADIUS_M`, default `250.0`).

- [ ] **Step 1: Write the failing classifier tests**

Append to `tests/test_classifier.py` — the fixture db has no gtfs tables, so a helper creates them; and geo observations use `record_observation` with coords:

```python
# 5 stops 400.3 m apart on a meridian, same geometry as Fixtureville.
GEO_COORDS = [(53.3000 + 0.0036 * i, -6.2000) for i in range(5)]


def geo_timetable(db, trip_id="T1", coords=GEO_COORDS):
    db.executescript(
        "CREATE TABLE IF NOT EXISTS gtfs_stop_times "
        "(trip_id TEXT, stop_sequence INTEGER, dep_seconds INTEGER, stop_id TEXT);"
        "CREATE TABLE IF NOT EXISTS gtfs_stops (stop_id TEXT PRIMARY KEY, lat REAL, lon REAL);")
    for seq, (lat, lon) in enumerate(coords, start=1):
        sid = f"{trip_id}_{seq}"
        db.execute("INSERT INTO gtfs_stop_times VALUES (?,?,?,?)", (trip_id, seq, 0, sid))
        db.execute("INSERT OR REPLACE INTO gtfs_stops VALUES (?,?,?)", (sid, lat, lon))


def geo_obs(db, trip, minutes_after_start, lat, lon):
    record_observation(db, trip.trip_id, str(DAY),
                       (trip.start_utc + dt.timedelta(minutes=minutes_after_start)).isoformat(),
                       "position", None, lat, lon)


def test_geo_completed_via_progress_branch(db):
    # Pings walk all 5 stops but the LAST ping is 30 min before scheduled end,
    # so the within-10-min-of-end time branch cannot fire: only geographic
    # progress (5/5 >= 0.90) can produce COMPLETED here.
    trip = make_trip(n_stops=5)
    beat_window(db, trip)
    geo_timetable(db)
    for i in range(5):
        geo_obs(db, trip, 5 + i * 6, *GEO_COORDS[i])  # minutes 5..29
    assert classify_trip(db, trip) == "COMPLETED"


def test_geo_vanished_early_silence(db):
    # Pings near stops 1-2 only (progress 0.4 < 0.75), silence from minute 15.
    trip = make_trip(n_stops=5)
    beat_window(db, trip)
    geo_timetable(db)
    geo_obs(db, trip, 5, *GEO_COORDS[0])
    geo_obs(db, trip, 15, *GEO_COORDS[1])
    assert classify_trip(db, trip) == "VANISHED"


def test_geo_off_route_pings_do_not_complete(db):
    # All pings far from every stop: no geo evidence, last ping early -> VANISHED
    # ... except progress is 0 < 0.75 and silence > 15 min, so VANISHED. An
    # implementation that snapped pings to the nearest stop regardless of radius
    # would instead reach COMPLETED via fabricated progress.
    trip = make_trip(n_stops=5)
    beat_window(db, trip)
    geo_timetable(db)
    geo_obs(db, trip, 5, 53.5000, -6.9000)
    geo_obs(db, trip, 20, 53.5010, -6.9000)
    assert classify_trip(db, trip) == "VANISHED"


def test_geo_query_survives_pre_refresh_db(db):
    # Coordinates present on observations but NO gtfs tables at all (live DB
    # between deploy and the first timetable refresh): must not crash, must
    # fall back to exactly the pre-G1 behavior.
    trip = make_trip(n_stops=5)
    beat_window(db, trip)
    geo_obs(db, trip, 15, 53.3036, -6.2000)
    assert classify_trip(db, trip) == "VANISHED"


def test_geo_and_seq_evidence_merge_by_max(db):
    # Feed seq says stop 1 (0.2); geo ping sits at stop 5 -> progress 1.0.
    trip = make_trip(n_stops=5)
    beat_window(db, trip)
    geo_timetable(db)
    obs(db, trip, 5, 1)
    geo_obs(db, trip, 25, *GEO_COORDS[4])
    assert classify_trip(db, trip) == "COMPLETED"


def test_tighter_radius_is_honoured(db):
    # Ping 177.9 m from stop 5: matches at the 250 m default, not at 100 m.
    trip = make_trip(n_stops=5)
    beat_window(db, trip)
    geo_timetable(db)
    geo_obs(db, trip, 25, GEO_COORDS[4][0] - 0.0016, GEO_COORDS[4][1])
    assert classify_trip(db, trip) == "COMPLETED"
    assert classify_trip(db, trip, radius_m=100.0) == "VANISHED"
```

- [ ] **Step 2: Write the failing config tests**

Append to `tests/test_config.py`:

```python
def test_read_match_radius_default(monkeypatch):
    monkeypatch.delenv("GHOSTBUS_MATCH_RADIUS_M", raising=False)
    assert cfg.read_match_radius_m() == 250.0


def test_read_match_radius_override(monkeypatch):
    monkeypatch.setenv("GHOSTBUS_MATCH_RADIUS_M", "150")
    assert cfg.read_match_radius_m() == 150.0
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_classifier.py tests/test_config.py -q`
Expected: new tests FAIL (missing `read_match_radius_m`, unexpected keyword `radius_m`, wrong outcomes); existing tests PASS.

- [ ] **Step 4: Implement classifier + config changes**

(a) `ghostbus_config.py` — add:

```python
DEFAULT_MATCH_RADIUS_M = 250.0
```

and:

```python
def read_match_radius_m() -> float:
    return float(os.environ.get("GHOSTBUS_MATCH_RADIUS_M", DEFAULT_MATCH_RADIUS_M))
```

(b) `classify/outcomes.py` — add import `from classify.progress import matched_max_seq`, add the guarded stops query, and rework `classify_trip`/`classify_day`. Full replacement for the two functions (the 250.0 literal default mirrors `ghostbus_config.DEFAULT_MATCH_RADIUS_M`; classify must not import runtime config):

```python
def _trip_stop_coords(db: sqlite3.Connection, trip_id: str) -> list[tuple[int, float, float]]:
    try:
        return db.execute(
            "SELECT st.stop_sequence, s.lat, s.lon FROM gtfs_stop_times st "
            "JOIN gtfs_stops s ON s.stop_id = st.stop_id WHERE st.trip_id=?",
            (trip_id,)).fetchall()
    except sqlite3.OperationalError:
        # Pre-refresh database (no gtfs_stops table / stop_id column yet):
        # geographic evidence is simply unavailable, never an error - progress
        # falls back to feed stop_sequence alone, i.e. pre-G1 behavior.
        return []


def classify_trip(db: sqlite3.Connection, trip: ScheduledTrip,
                  radius_m: float = 250.0) -> str:
    if uptime(db, trip.window_start_utc, trip.window_end_utc) < 0.90:
        return "EXCLUDED"
    rows = db.execute(
        "SELECT ts_utc, kind, stop_sequence, lat, lon FROM observations "
        "WHERE trip_id=? AND service_date=? AND ts_utc>=? AND ts_utc<? ORDER BY ts_utc",
        (trip.trip_id, str(trip.service_date),
         trip.window_start_utc.isoformat(), trip.window_end_utc.isoformat())).fetchall()
    if any(kind == "cancel" for _, kind, _, _, _ in rows):
        return "CANCELLED"
    tracked = [(ts, seq, lat, lon) for ts, kind, seq, lat, lon in rows if kind == "position"]
    if not tracked:
        return "UNTRACKED"
    # Parse before comparing - string order breaks if timestamp formats ever vary.
    last_ts = max(dt.datetime.fromisoformat(ts) for ts, _, _, _ in tracked)
    seqs = [seq for _, seq, _, _ in tracked if seq is not None]
    # Geographic evidence (amendment G1): GPS pings matched to the trip's own
    # scheduled stops. Merges with feed stop_sequence by max - it can only
    # RAISE progress, never lower it or affect any other class.
    pings = [(lat, lon) for _, _, lat, lon in tracked if lat is not None and lon is not None]
    if pings:
        geo_seq = matched_max_seq(_trip_stop_coords(db, trip.trip_id), pings, radius_m)
        if geo_seq is not None:
            seqs.append(geo_seq)
    # GTFS stop_sequence need not be contiguous, so the denominator is the trip's
    # own max scheduled sequence, clamped defensively.
    progress = min(1.0, max(seqs) / trip.max_stop_seq) if seqs else 0.0
    if progress >= 0.90 or last_ts >= trip.end_utc - dt.timedelta(minutes=10):
        return "COMPLETED"
    if progress < 0.75 and last_ts < trip.end_utc - dt.timedelta(minutes=15):
        return "VANISHED"
    # Residual: neither clearly completed nor vanished (incl. any-progress trips last
    # seen 10-15 min before scheduled end) - benefit of the doubt goes to the operator.
    return "COMPLETED"


def classify_day(db: sqlite3.Connection, trips: list[ScheduledTrip],
                 now_utc: dt.datetime, radius_m: float = 250.0) -> dict[str, str]:
    db.executescript(_OUTCOMES_SCHEMA)
    results: dict[str, str] = {}
    for trip in trips:
        if trip.window_end_utc > now_utc:
            continue
        outcome = classify_trip(db, trip, radius_m)
        results[trip.trip_id] = outcome
        db.execute("INSERT OR REPLACE INTO trip_outcomes VALUES (?,?,?,?,?)",
                   (trip.trip_id, str(trip.service_date), trip.route_id,
                    trip.start_utc.isoformat(), outcome))
    db.commit()
    return results
```

Also update the module docstring's COMPLETED/VANISHED/UNTRACKED lines to mention that progress evidence = feed stop_sequence ∪ geographic nearest-stop matching (G1).

(c) `classify/run_classifier.py` — thread the radius and make the classifier self-migrating (`init_store` is idempotent; without it, a classifier run between deploy and poller restart would crash on the new lat/lon columns):

```python
from classify.store import init_store
from ghostbus_config import get_db, read_agency_names, read_match_radius_m
```

`run_for_dates` gains `radius_m: float = 250.0` as final parameter; its `classify_day` call becomes `classify_day(db, trips, now_utc, radius_m)`. In `main()`:

```python
    db = get_db()
    init_store(db)
    agency_names = read_agency_names()
    radius_m = read_match_radius_m()
```

and the call becomes `run_for_dates(db, dates, agency_names, now_utc, radius_m)`.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: all tests PASS (~66 total).

- [ ] **Step 6: Commit**

```bash
git add classify/outcomes.py classify/run_classifier.py ghostbus_config.py tests/test_classifier.py tests/test_config.py
git commit -m "feat(classify): geographic progress evidence with configurable match radius (G1)"
```

---

### Task 6: Documentation — README amendment G1 + runbook upgrade section

**Files:**
- Modify: `README.md` (methodology + known-limitations sections — read the file first, slot into its existing structure and heading style)
- Modify: `ops/RUNBOOK.md` (append an upgrade section)

**Interfaces:**
- Consumes: everything above, descriptively.
- Produces: public methodology record of amendment G1.

- [ ] **Step 1: README — methodology amendment**

Add under the methodology/classification section, matching the README's existing tone:

```markdown
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
```

- [ ] **Step 2: README — known limitations addition**

Add to the known-limitations list:

```markdown
- Nearest-stop matching is coarse: two physically close stops (loops,
  opposite roadsides) can credit the wrong sequence. At the 75%/90%
  thresholds this is noise rather than systematic bias; the burn-in
  quantifies the geo-match rate before any number is published.
```

- [ ] **Step 3: RUNBOOK — upgrade section**

Append (adjust section numbering/commands to the runbook's existing conventions — read it first):

```markdown
## Upgrade to geographic progress (G1, 2026-07-19)

Order-independent - each step degrades gracefully until the others run -
but this sequence starts coordinate capture soonest:

1. `cd /opt/ghost-bus && git pull`
2. Restart the poller (`systemctl restart ghostbus-poller`): init_store
   adds lat/lon to observations; pings carry coordinates from this moment.
3. Run the timetable refresh once (per the refresh procedure above) to
   load stop coordinates, stop_ids and route display names.
4. Nothing else - the classifier timer picks up geographic progress on its
   next run. Optional: set GHOSTBUS_MATCH_RADIUS_M in /etc/ghostbus.env
   (default 250) once burn-in data suggests a better radius.
```

- [ ] **Step 4: Run the full suite (docs must not break anything)**

Run: `python -m pytest -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add README.md ops/RUNBOOK.md
git commit -m "docs: spec amendment G1 - geographic progress methodology and upgrade runbook"
```

**Post-plan session tasks (owner, not the task executor):** update vault `19-ghost-bus` (_INDEX status, KNOWN_ISSUES: mark the geo-progress item addressed, add geo-match-rate to the burn-in checklist, note route-names loader fix); deploy to the VM per the new runbook section when Alex approves.
