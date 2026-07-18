# Ghost Bus Tracker — Core Pipeline Implementation Plan (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The complete offline core of the Dublin Ghost Bus Tracker: GTFS timetable engine, five-class trip outcome classifier, aggregates, publish gate, and an offline-testable GTFS-R poller — all TDD against a synthetic "Fixtureville" network. No API key, no VM, no network in tests.

**Architecture:** Plain Python 3.12 packages (`timetable/`, `classify/`, `aggregate/`, `ingest/`), SQLite state, stdlib-first (csv/zipfile/zoneinfo/sqlite3); protobuf only for GTFS-R parsing. Spec: `docs/superpowers/specs/2026-07-18-ghost-bus-design.md` — its taxonomy table is the normative rule set.

**Tech Stack:** Python 3.12, sqlite3, zoneinfo, gtfs-realtime-bindings, zstandard, requests, pytest, GitHub Actions (checkout@v5 / setup-python@v6).

## Global Constraints

- Repo root: the ghost-bus repo (spec + this plan committed). Windows dev machine: all files UTF-8, use Write/Edit tools, never shell heredocs.
- Tests NEVER touch the network. The poller's fetch is injected (`fetch_fn`) so tests feed it synthetic protobufs.
- All times stored in UTC (ISO 8601, `+00:00`); GTFS local times converted via the GTFS rule: **seconds after (service-day noon minus 12 h) in Europe/Dublin** — this handles both >24:00:00 times and DST correctly. Never add seconds to local midnight.
- Outcome classes, exactly these strings: `EXCLUDED`, `CANCELLED`, `COMPLETED`, `VANISHED`, `UNTRACKED`. Precedence is that order, first match wins.
- Thresholds (spec, verbatim): window = start−5min → end+15min; EXCLUDED if uptime < 0.90; COMPLETED if progress ≥ 0.90 or last obs within 10 min of scheduled end; VANISHED if observed and last obs has progress < 0.75 and is > 15 min before scheduled end; else UNTRACKED (zero observations).
- Poll cadence assumption everywhere: one heartbeat expected per 60 s.
- Commits: conventional messages ending `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Scaffold + Fixtureville GTFS builder

**Files:**
- Create: `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `.gitignore`, `.gitattributes`, `.github/workflows/tests.yml`, `timetable/__init__.py`, `classify/__init__.py`, `aggregate/__init__.py`, `ingest/__init__.py`, `tests/__init__.py`, `tests/fixtureville.py`, `tests/test_fixtureville.py`

**Interfaces:**
- Produces: `tests.fixtureville.build_gtfs_zip(path) -> None` writing a valid GTFS zip; `tests.fixtureville.FIXTURE_TZ = "Europe/Dublin"`; route ids `R1`,`R2`; service ids `WK` (Mon–Fri 2026-03-23..2026-04-10), `SAT` (Sat only, same range); trip ids `R1_wk_00`..`R1_wk_09` (10 weekday R1 trips, half-hourly from 07:00:00, 5 stops each, 60 min duration), `R1_late` (departs `24:30:00`, WK), `R2_wk_00`..`R2_wk_04` (5 weekday R2 trips from 08:15:00, 4 stops, 45 min), `R2_sat_00` (SAT 09:00:00).

- [ ] **Step 1: Scaffold files**

`requirements.txt`:
```
gtfs-realtime-bindings>=1.0
zstandard>=0.22
requests>=2.32
```
`requirements-dev.txt`:
```
-r requirements.txt
pytest>=8.0
```
`pytest.ini`:
```ini
[pytest]
testpaths = tests
addopts = -q
```
`.gitignore`:
```
__pycache__/
*.pyc
.venv/
.pytest_cache/
.superpowers/
data/
state/
```
`.gitattributes`:
```
* text=auto eol=lf
```
`.github/workflows/tests.yml`:
```yaml
name: tests
on:
  push: {branches: [main]}
  pull_request:
  workflow_dispatch:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with: {python-version: '3.12', cache: pip}
      - run: pip install -r requirements-dev.txt
      - run: python -m pytest
```
All `__init__.py` files: empty.

- [ ] **Step 2: Write the failing test** — `tests/test_fixtureville.py`:

```python
import csv
import io
import zipfile

from tests.fixtureville import build_gtfs_zip

REQUIRED = ["agency.txt", "stops.txt", "routes.txt", "trips.txt",
            "stop_times.txt", "calendar.txt"]


def read(zf: zipfile.ZipFile, name: str) -> list[dict]:
    with zf.open(name) as fh:
        return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8")))


def test_zip_contains_required_files(tmp_path):
    path = tmp_path / "fixtureville.zip"
    build_gtfs_zip(path)
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
    assert set(REQUIRED) <= names


def test_trip_and_service_shape(tmp_path):
    path = tmp_path / "fixtureville.zip"
    build_gtfs_zip(path)
    with zipfile.ZipFile(path) as zf:
        trips = read(zf, "trips.txt")
        stop_times = read(zf, "stop_times.txt")
        calendar = read(zf, "calendar.txt")
    trip_ids = {t["trip_id"] for t in trips}
    assert {"R1_late", "R1_wk_00", "R2_wk_00", "R2_sat_00"} <= trip_ids
    assert len([t for t in trips if t["route_id"] == "R1"]) == 11  # 10 + late
    late_times = [st for st in stop_times if st["trip_id"] == "R1_late"]
    assert late_times[0]["departure_time"] == "24:30:00"  # past-midnight trip
    services = {c["service_id"]: c for c in calendar}
    assert services["WK"]["monday"] == "1" and services["WK"]["saturday"] == "0"
    assert services["SAT"]["saturday"] == "1" and services["SAT"]["monday"] == "0"
    assert services["WK"]["start_date"] == "20260323"
    assert services["WK"]["end_date"] == "20260410"


def test_every_trip_has_ordered_stop_times(tmp_path):
    path = tmp_path / "fixtureville.zip"
    build_gtfs_zip(path)
    with zipfile.ZipFile(path) as zf:
        stop_times = read(zf, "stop_times.txt")
    by_trip: dict[str, list[int]] = {}
    for st in stop_times:
        by_trip.setdefault(st["trip_id"], []).append(int(st["stop_sequence"]))
    for trip_id, seqs in by_trip.items():
        assert seqs == sorted(seqs) and len(seqs) >= 4, trip_id
```

- [ ] **Step 3: Run to verify failure** — `python -m pytest tests/test_fixtureville.py -v` → FAIL (no module).

- [ ] **Step 4: Implement** — `tests/fixtureville.py`:

```python
"""Synthetic GTFS network for tests: 2 routes, WK+SAT services, a past-midnight
trip, valid across the 2026-03-29 DST change. Deterministic, built in-memory."""
from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

FIXTURE_TZ = "Europe/Dublin"

_STOPS_R1 = ["S1", "S2", "S3", "S4", "S5"]
_STOPS_R2 = ["S2", "S4", "S6", "S7"]


def _hms(total_seconds: int) -> str:
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _trip_rows(trip_id: str, start_s: int, duration_s: int, stops: list[str]):
    n = len(stops)
    step = duration_s // (n - 1)
    for seq, stop in enumerate(stops, start=1):
        t = _hms(start_s + (seq - 1) * step)
        yield {"trip_id": trip_id, "arrival_time": t, "departure_time": t,
               "stop_id": stop, "stop_sequence": str(seq)}


def build_gtfs_zip(path: str | Path) -> None:
    agency = [{"agency_id": "FVB", "agency_name": "Fixtureville Bus",
               "agency_url": "https://example.invalid", "agency_timezone": FIXTURE_TZ}]
    stops = [{"stop_id": s, "stop_name": f"Stop {s}", "stop_lat": "53.3", "stop_lon": "-6.2"}
             for s in sorted(set(_STOPS_R1 + _STOPS_R2))]
    routes = [{"route_id": "R1", "agency_id": "FVB", "route_short_name": "1",
               "route_long_name": "Fixtureville Main", "route_type": "3"},
              {"route_id": "R2", "agency_id": "FVB", "route_short_name": "2",
               "route_long_name": "Fixtureville Orbital", "route_type": "3"}]
    calendar = [
        {"service_id": "WK", "monday": "1", "tuesday": "1", "wednesday": "1",
         "thursday": "1", "friday": "1", "saturday": "0", "sunday": "0",
         "start_date": "20260323", "end_date": "20260410"},
        {"service_id": "SAT", "monday": "0", "tuesday": "0", "wednesday": "0",
         "thursday": "0", "friday": "0", "saturday": "1", "sunday": "0",
         "start_date": "20260323", "end_date": "20260410"},
    ]
    trips, stop_times = [], []

    def add_trip(trip_id, route_id, service_id, start_s, duration_s, stop_list):
        trips.append({"trip_id": trip_id, "route_id": route_id, "service_id": service_id})
        stop_times.extend(_trip_rows(trip_id, start_s, duration_s, stop_list))

    for i in range(10):  # half-hourly from 07:00, 60-minute run
        add_trip(f"R1_wk_{i:02d}", "R1", "WK", 7 * 3600 + i * 1800, 3600, _STOPS_R1)
    add_trip("R1_late", "R1", "WK", 24 * 3600 + 1800, 3600, _STOPS_R1)  # 24:30:00
    for i in range(5):  # from 08:15, 45-minute run
        add_trip(f"R2_wk_{i:02d}", "R2", "WK", 8 * 3600 + 900 + i * 3600, 2700, _STOPS_R2)
    add_trip("R2_sat_00", "R2", "SAT", 9 * 3600, 2700, _STOPS_R2)

    tables = {"agency.txt": agency, "stops.txt": stops, "routes.txt": routes,
              "trips.txt": trips, "stop_times.txt": stop_times, "calendar.txt": calendar}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, rows in tables.items():
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
            zf.writestr(name, buf.getvalue())
```

- [ ] **Step 5: Run to verify pass** — `python -m pytest tests/test_fixtureville.py -v` → PASS.
- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat: scaffold + Fixtureville synthetic GTFS builder"`

---

### Task 2: Timetable engine (GTFS load + service-day expansion)

**Files:**
- Create: `timetable/gtfs.py`, `tests/test_timetable.py`

**Interfaces:**
- Consumes: `tests.fixtureville.build_gtfs_zip`.
- Produces: `timetable.gtfs.load_gtfs(zip_path, db) -> str` (streams GTFS csv into SQLite tables `gtfs_trips(trip_id, route_id, service_id)`, `gtfs_stop_times(trip_id, stop_sequence, dep_seconds)`, `gtfs_calendar(...)`, `gtfs_meta(key, value)`; returns sha256 hash of the zip, stored in `gtfs_meta['gtfs_hash']`); `timetable.gtfs.gtfs_seconds(hms: str) -> int`; `timetable.gtfs.local_from_service(service_date: datetime.date, seconds: int, tz: str) -> datetime` (aware, implements the noon-minus-12h rule); `timetable.gtfs.scheduled_trips(db, service_date, tz="Europe/Dublin") -> list[ScheduledTrip]` where `ScheduledTrip` is a dataclass with `trip_id, route_id, service_date, start_utc, end_utc, window_start_utc, window_end_utc, n_stops` (window = start−5 min / end+15 min, all aware UTC datetimes).

- [ ] **Step 1: Write the failing tests** — `tests/test_timetable.py`:

```python
import datetime as dt
import sqlite3
import zoneinfo

import pytest

from tests.fixtureville import build_gtfs_zip
from timetable.gtfs import gtfs_seconds, load_gtfs, local_from_service, scheduled_trips

UTC = dt.timezone.utc


@pytest.fixture()
def db(tmp_path):
    conn = sqlite3.connect(":memory:")
    zip_path = tmp_path / "f.zip"
    build_gtfs_zip(zip_path)
    load_gtfs(zip_path, conn)
    return conn


def test_gtfs_seconds_handles_past_midnight():
    assert gtfs_seconds("07:00:00") == 25200
    assert gtfs_seconds("24:30:00") == 88200


def test_load_stores_hash_and_counts(db):
    (h,) = db.execute("SELECT value FROM gtfs_meta WHERE key='gtfs_hash'").fetchone()
    assert len(h) == 64
    (n_trips,) = db.execute("SELECT COUNT(*) FROM gtfs_trips").fetchone()
    assert n_trips == 17  # 11 R1 + 5 R2 wk + 1 R2 sat


def test_noon_rule_regular_day():
    # Mon 2026-03-23, no DST that day: 07:00:00 -> 07:00 local
    local = local_from_service(dt.date(2026, 3, 23), gtfs_seconds("07:00:00"), "Europe/Dublin")
    assert local.hour == 7 and local.utcoffset() == dt.timedelta(0)  # GMT


def test_noon_rule_past_midnight_lands_next_day():
    local = local_from_service(dt.date(2026, 3, 23), gtfs_seconds("24:30:00"), "Europe/Dublin")
    assert local.date() == dt.date(2026, 3, 24) and local.hour == 0 and local.minute == 30


def test_noon_rule_dst_spring_forward():
    # Sat service 2026-03-28: its 24:30 trip runs during the 2026-03-29 01:00 spring-forward.
    # Noon-minus-12h rule: base = 2026-03-28 12:00 IST-boundary-safe; 24:30 = base+12.5h
    # -> 2026-03-29 01:30 local DOES NOT EXIST (clocks jump 01:00->02:00), rule yields 02:30 IST.
    local = local_from_service(dt.date(2026, 3, 28), gtfs_seconds("24:30:00"), "Europe/Dublin")
    assert local.utcoffset() == dt.timedelta(hours=1)  # IST after the jump
    assert local.astimezone(UTC).hour == 1 and local.astimezone(UTC).minute == 30


def test_scheduled_trips_weekday(db):
    trips = scheduled_trips(db, dt.date(2026, 3, 23))
    ids = {t.trip_id for t in trips}
    assert "R1_wk_00" in ids and "R1_late" in ids and "R2_sat_00" not in ids
    assert len(trips) == 16
    t0 = next(t for t in trips if t.trip_id == "R1_wk_00")
    assert t0.start_utc == dt.datetime(2026, 3, 23, 7, 0, tzinfo=UTC)  # GMT day
    assert t0.window_start_utc == t0.start_utc - dt.timedelta(minutes=5)
    assert t0.window_end_utc == t0.end_utc + dt.timedelta(minutes=15)
    assert t0.n_stops == 5


def test_scheduled_trips_saturday(db):
    trips = scheduled_trips(db, dt.date(2026, 3, 28))
    assert {t.trip_id for t in trips} == {"R2_sat_00"}


def test_out_of_range_date_is_empty(db):
    assert scheduled_trips(db, dt.date(2026, 5, 1)) == []
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_timetable.py -v` → FAIL.

- [ ] **Step 3: Implement** — `timetable/gtfs.py`:

```python
"""GTFS static timetable: streaming load into SQLite + service-day expansion.

Time rule: a GTFS time is seconds after (service-day noon local minus 12 h) —
the canonical way to survive DST changes and >24:00:00 departures.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gtfs_trips (trip_id TEXT PRIMARY KEY, route_id TEXT, service_id TEXT);
CREATE TABLE IF NOT EXISTS gtfs_stop_times (trip_id TEXT, stop_sequence INTEGER, dep_seconds INTEGER);
CREATE TABLE IF NOT EXISTS gtfs_calendar (
  service_id TEXT PRIMARY KEY, monday INT, tuesday INT, wednesday INT, thursday INT,
  friday INT, saturday INT, sunday INT, start_date TEXT, end_date TEXT);
CREATE TABLE IF NOT EXISTS gtfs_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_stop_times_trip ON gtfs_stop_times(trip_id);
"""


def gtfs_seconds(hms: str) -> int:
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def local_from_service(service_date: dt.date, seconds: int, tz: str) -> dt.datetime:
    zone = ZoneInfo(tz)
    noon = dt.datetime(service_date.year, service_date.month, service_date.day,
                       12, 0, tzinfo=zone)
    return (noon - dt.timedelta(hours=12) + dt.timedelta(seconds=seconds)).astimezone(zone)


def load_gtfs(zip_path: str | Path, db: sqlite3.Connection) -> str:
    digest = hashlib.sha256(Path(zip_path).read_bytes()).hexdigest()
    db.executescript(_SCHEMA)
    with zipfile.ZipFile(zip_path) as zf:
        def rows(name):
            with zf.open(name) as fh:
                yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))

        db.execute("DELETE FROM gtfs_trips"); db.execute("DELETE FROM gtfs_stop_times")
        db.execute("DELETE FROM gtfs_calendar")
        db.executemany("INSERT INTO gtfs_trips VALUES (?,?,?)",
                       [(r["trip_id"], r["route_id"], r["service_id"]) for r in rows("trips.txt")])
        db.executemany("INSERT INTO gtfs_stop_times VALUES (?,?,?)",
                       [(r["trip_id"], int(r["stop_sequence"]), gtfs_seconds(r["departure_time"]))
                        for r in rows("stop_times.txt")])
        db.executemany("INSERT INTO gtfs_calendar VALUES (?,?,?,?,?,?,?,?,?,?)",
                       [(r["service_id"], *[int(r[d]) for d in
                         ("monday", "tuesday", "wednesday", "thursday", "friday",
                          "saturday", "sunday")], r["start_date"], r["end_date"])
                        for r in rows("calendar.txt")])
    db.execute("INSERT OR REPLACE INTO gtfs_meta VALUES ('gtfs_hash', ?)", (digest,))
    db.commit()
    return digest


@dataclass(frozen=True)
class ScheduledTrip:
    trip_id: str
    route_id: str
    service_date: dt.date
    start_utc: dt.datetime
    end_utc: dt.datetime
    window_start_utc: dt.datetime
    window_end_utc: dt.datetime
    n_stops: int


_WEEKDAY_COLS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def scheduled_trips(db: sqlite3.Connection, service_date: dt.date,
                    tz: str = "Europe/Dublin") -> list[ScheduledTrip]:
    datestr = service_date.strftime("%Y%m%d")
    col = _WEEKDAY_COLS[service_date.weekday()]
    services = {sid for (sid,) in db.execute(
        f"SELECT service_id FROM gtfs_calendar WHERE {col}=1 AND start_date<=? AND end_date>=?",
        (datestr, datestr))}
    if not services:
        return []
    out: list[ScheduledTrip] = []
    marks = ",".join("?" * len(services))
    for trip_id, route_id in db.execute(
            f"SELECT trip_id, route_id FROM gtfs_trips WHERE service_id IN ({marks})",
            tuple(services)):
        row = db.execute(
            "SELECT MIN(dep_seconds), MAX(dep_seconds), COUNT(*) FROM gtfs_stop_times WHERE trip_id=?",
            (trip_id,)).fetchone()
        first_s, last_s, n_stops = row
        start = local_from_service(service_date, first_s, tz).astimezone(UTC)
        end = local_from_service(service_date, last_s, tz).astimezone(UTC)
        out.append(ScheduledTrip(
            trip_id, route_id, service_date, start, end,
            start - dt.timedelta(minutes=5), end + dt.timedelta(minutes=15), n_stops))
    out.sort(key=lambda t: (t.start_utc, t.trip_id))
    return out
```

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_timetable.py -v` → PASS; then full suite.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: GTFS timetable engine with noon-rule service-day expansion"`

---

### Task 3: Observation store + heartbeat uptime

**Files:**
- Create: `classify/store.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `classify.store.init_store(db)` (tables `observations(trip_id TEXT, service_date TEXT, ts_utc TEXT, kind TEXT, stop_sequence INTEGER)` with kind ∈ {'position','update','cancel'}, and `heartbeats(ts_utc TEXT PRIMARY KEY, ok INTEGER)`); `classify.store.record_heartbeat(db, ts_utc, ok)`; `classify.store.record_observation(db, trip_id, service_date, ts_utc, kind, stop_sequence=None)`; `classify.store.uptime(db, start_utc, end_utc) -> float` (fraction of expected 60 s slots in [start,end) that have an ok heartbeat; expected slots = ceil(window_seconds/60), matched by counting DISTINCT ok heartbeats whose ts falls in the window; returns 0.0 for empty windows).

- [ ] **Step 1: Write the failing tests** — `tests/test_store.py`:

```python
import datetime as dt
import sqlite3

import pytest

from classify.store import init_store, record_heartbeat, record_observation, uptime

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 3, 23, 7, 0, tzinfo=UTC)


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    init_store(conn)
    return conn


def fill_heartbeats(db, start, minutes, ok=True, skip=()):
    for i in range(minutes):
        if i in skip:
            continue
        record_heartbeat(db, (start + dt.timedelta(minutes=i)).isoformat(), ok)


def test_full_heartbeats_give_uptime_1(db):
    fill_heartbeats(db, T0, 60)
    assert uptime(db, T0, T0 + dt.timedelta(hours=1)) == pytest.approx(1.0)


def test_half_missing_heartbeats(db):
    fill_heartbeats(db, T0, 60, skip=set(range(0, 60, 2)))
    assert uptime(db, T0, T0 + dt.timedelta(hours=1)) == pytest.approx(0.5)


def test_failed_polls_do_not_count(db):
    fill_heartbeats(db, T0, 60, ok=False)
    assert uptime(db, T0, T0 + dt.timedelta(hours=1)) == 0.0


def test_uptime_clipped_to_window(db):
    fill_heartbeats(db, T0 - dt.timedelta(hours=2), 240)  # covers well beyond
    assert uptime(db, T0, T0 + dt.timedelta(hours=1)) == pytest.approx(1.0, abs=0.02)


def test_observation_roundtrip(db):
    record_observation(db, "R1_wk_00", "2026-03-23", T0.isoformat(), "position", 3)
    rows = db.execute("SELECT trip_id, kind, stop_sequence FROM observations").fetchall()
    assert rows == [("R1_wk_00", "position", 3)]
```

- [ ] **Step 2: Run to verify failure**, then **Step 3: Implement** — `classify/store.py`:

```python
"""SQLite observation + heartbeat store. All timestamps ISO-8601 UTC strings."""
from __future__ import annotations

import datetime as dt
import math
import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
  trip_id TEXT, service_date TEXT, ts_utc TEXT, kind TEXT, stop_sequence INTEGER);
CREATE INDEX IF NOT EXISTS idx_obs_trip ON observations(trip_id, service_date);
CREATE TABLE IF NOT EXISTS heartbeats (ts_utc TEXT PRIMARY KEY, ok INTEGER);
"""


def init_store(db: sqlite3.Connection) -> None:
    db.executescript(_SCHEMA)
    db.commit()


def record_heartbeat(db: sqlite3.Connection, ts_utc: str, ok: bool) -> None:
    db.execute("INSERT OR REPLACE INTO heartbeats VALUES (?,?)", (ts_utc, int(ok)))
    db.commit()


def record_observation(db: sqlite3.Connection, trip_id: str, service_date: str,
                       ts_utc: str, kind: str, stop_sequence: int | None = None) -> None:
    if kind not in ("position", "update", "cancel"):
        raise ValueError(f"unknown observation kind {kind!r}")
    db.execute("INSERT INTO observations VALUES (?,?,?,?,?)",
               (trip_id, service_date, ts_utc, kind, stop_sequence))
    db.commit()


def uptime(db: sqlite3.Connection, start_utc: dt.datetime, end_utc: dt.datetime) -> float:
    window_s = (end_utc - start_utc).total_seconds()
    if window_s <= 0:
        return 0.0
    expected = math.ceil(window_s / 60.0)
    (got,) = db.execute(
        "SELECT COUNT(*) FROM heartbeats WHERE ok=1 AND ts_utc>=? AND ts_utc<?",
        (start_utc.isoformat(), end_utc.isoformat())).fetchone()
    return min(1.0, got / expected)
```

- [ ] **Step 4: Verify pass + full suite. Step 5: Commit** — `git add -A && git commit -m "feat: observation store and heartbeat uptime"`

---

### Task 4: The classifier (the core)

**Files:**
- Create: `classify/outcomes.py`, `tests/test_classifier.py`

**Interfaces:**
- Consumes: `ScheduledTrip` (Task 2), store tables (Task 3).
- Produces: `classify.outcomes.OUTCOMES = ("EXCLUDED","CANCELLED","COMPLETED","VANISHED","UNTRACKED")`; `classify.outcomes.classify_trip(db, trip: ScheduledTrip) -> str` implementing the spec taxonomy with exact precedence; `classify.outcomes.classify_day(db, trips: list[ScheduledTrip], now_utc) -> dict[str, str]` (classifies only trips whose `window_end_utc <= now_utc`; returns {trip_id: outcome}; writes rows into table `trip_outcomes(trip_id, service_date, route_id, outcome, gtfs_hash?)` — create table `trip_outcomes(trip_id TEXT, service_date TEXT, route_id TEXT, start_utc TEXT, outcome TEXT, PRIMARY KEY (trip_id, service_date))` with INSERT OR REPLACE for idempotency).

- [ ] **Step 1: Write the failing tests** — `tests/test_classifier.py`:

```python
import datetime as dt
import sqlite3

import pytest

from classify.outcomes import OUTCOMES, classify_day, classify_trip
from classify.store import init_store, record_heartbeat, record_observation
from timetable.gtfs import ScheduledTrip

UTC = dt.timezone.utc
DAY = dt.date(2026, 3, 23)


def make_trip(trip_id="T1", start_h=7, dur_min=60, n_stops=5):
    start = dt.datetime(2026, 3, 23, start_h, 0, tzinfo=UTC)
    end = start + dt.timedelta(minutes=dur_min)
    return ScheduledTrip(trip_id, "R1", DAY, start, end,
                         start - dt.timedelta(minutes=5), end + dt.timedelta(minutes=15), n_stops)


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    init_store(conn)
    return conn


def beat_window(db, trip, skip_fraction=0.0):
    t, i = trip.window_start_utc, 0
    while t < trip.window_end_utc:
        if not (skip_fraction and i % int(1 / skip_fraction) == 0):
            record_heartbeat(db, t.isoformat(), True)
        t += dt.timedelta(minutes=1)
        i += 1


def obs(db, trip, minutes_after_start, seq):
    record_observation(db, trip.trip_id, str(DAY),
                       (trip.start_utc + dt.timedelta(minutes=minutes_after_start)).isoformat(),
                       "position", seq)


def test_excluded_when_tracker_down(db):
    trip = make_trip()  # no heartbeats at all -> uptime 0
    assert classify_trip(db, trip) == "EXCLUDED"


def test_cancelled_beats_everything_after_exclusion(db):
    trip = make_trip()
    beat_window(db, trip)
    record_observation(db, trip.trip_id, str(DAY), trip.start_utc.isoformat(), "cancel")
    obs(db, trip, 10, 5)  # even with full-progress observations...
    assert classify_trip(db, trip) == "CANCELLED"


def test_completed_by_progress(db):
    trip = make_trip(n_stops=5)
    beat_window(db, trip)
    obs(db, trip, 5, 1); obs(db, trip, 30, 3); obs(db, trip, 55, 5)  # 5/5 = 100%
    assert classify_trip(db, trip) == "COMPLETED"


def test_completed_by_time_near_end(db):
    trip = make_trip(n_stops=10)
    beat_window(db, trip)
    obs(db, trip, 55, 6)  # progress 60% but last obs within 10 min of end
    assert classify_trip(db, trip) == "COMPLETED"


def test_vanished_mid_route(db):
    trip = make_trip(n_stops=5, dur_min=60)
    beat_window(db, trip)
    obs(db, trip, 5, 1); obs(db, trip, 15, 2)  # 40%, last obs 45 min before end
    assert classify_trip(db, trip) == "VANISHED"


def test_untracked_when_no_signal(db):
    trip = make_trip()
    beat_window(db, trip)
    assert classify_trip(db, trip) == "UNTRACKED"


def test_every_trip_gets_exactly_one_outcome(db):
    trips = [make_trip(f"T{i}", start_h=7 + i % 3) for i in range(12)]
    for i, t in enumerate(trips):
        if i % 4 != 0:
            beat_window(db, t)
        if i % 3 == 0:
            obs(db, t, 10, 5)
    now = dt.datetime(2026, 3, 24, tzinfo=UTC)
    result = classify_day(db, trips, now)
    assert set(result) == {t.trip_id for t in trips}
    assert all(o in OUTCOMES for o in result.values())


def test_classify_day_skips_open_windows_and_is_idempotent(db):
    trip = make_trip()
    beat_window(db, trip)
    early = classify_day(db, [trip], trip.window_end_utc - dt.timedelta(minutes=1))
    assert early == {}
    r1 = classify_day(db, [trip], trip.window_end_utc + dt.timedelta(minutes=1))
    r2 = classify_day(db, [trip], trip.window_end_utc + dt.timedelta(minutes=1))
    assert r1 == r2 == {trip.trip_id: "UNTRACKED"}
    (n,) = db.execute("SELECT COUNT(*) FROM trip_outcomes").fetchone()
    assert n == 1


def test_more_downtime_never_improves_stats(db):
    # EXCLUDED monotonicity: downgrading heartbeats can only move a trip to EXCLUDED,
    # never from a bad class to a good one.
    trip = make_trip()
    beat_window(db, trip)
    assert classify_trip(db, trip) == "UNTRACKED"
    db.execute("DELETE FROM heartbeats")
    assert classify_trip(db, trip) == "EXCLUDED"
```

- [ ] **Step 2: Run to verify failure**, then **Step 3: Implement** — `classify/outcomes.py`:

```python
"""Trip outcome classification — the spec's taxonomy, precedence order, first match wins.

EXCLUDED   tracker uptime < 90% of the trip window (our fault, not the operator's)
CANCELLED  feed marked the trip CANCELED during the window
COMPLETED  progress >= 90% of stops OR last observation within 10 min of scheduled end
VANISHED   observed, then silent with progress < 75% and > 15 min still to run
UNTRACKED  zero observations in the window (reported as untracked, not "did not run")
"""
from __future__ import annotations

import datetime as dt
import sqlite3

from classify.store import uptime
from timetable.gtfs import ScheduledTrip

OUTCOMES = ("EXCLUDED", "CANCELLED", "COMPLETED", "VANISHED", "UNTRACKED")

_OUTCOMES_SCHEMA = """
CREATE TABLE IF NOT EXISTS trip_outcomes (
  trip_id TEXT, service_date TEXT, route_id TEXT, start_utc TEXT, outcome TEXT,
  PRIMARY KEY (trip_id, service_date));
"""


def classify_trip(db: sqlite3.Connection, trip: ScheduledTrip) -> str:
    if uptime(db, trip.window_start_utc, trip.window_end_utc) < 0.90:
        return "EXCLUDED"
    rows = db.execute(
        "SELECT ts_utc, kind, stop_sequence FROM observations "
        "WHERE trip_id=? AND service_date=? AND ts_utc>=? AND ts_utc<? ORDER BY ts_utc",
        (trip.trip_id, str(trip.service_date),
         trip.window_start_utc.isoformat(), trip.window_end_utc.isoformat())).fetchall()
    if any(kind == "cancel" for _, kind, _ in rows):
        return "CANCELLED"
    tracked = [(ts, seq) for ts, kind, seq in rows if kind in ("position", "update")]
    if not tracked:
        return "UNTRACKED"
    last_ts = dt.datetime.fromisoformat(max(ts for ts, _ in tracked))
    seqs = [seq for _, seq in tracked if seq is not None]
    progress = (max(seqs) / trip.n_stops) if seqs else 0.0
    if progress >= 0.90 or last_ts >= trip.end_utc - dt.timedelta(minutes=10):
        return "COMPLETED"
    if progress < 0.75 and last_ts < trip.end_utc - dt.timedelta(minutes=15):
        return "VANISHED"
    return "COMPLETED"  # 75-90% progress ending late-window: benefit of the doubt


def classify_day(db: sqlite3.Connection, trips: list[ScheduledTrip],
                 now_utc: dt.datetime) -> dict[str, str]:
    db.executescript(_OUTCOMES_SCHEMA)
    results: dict[str, str] = {}
    for trip in trips:
        if trip.window_end_utc > now_utc:
            continue
        outcome = classify_trip(db, trip)
        results[trip.trip_id] = outcome
        db.execute("INSERT OR REPLACE INTO trip_outcomes VALUES (?,?,?,?,?)",
                   (trip.trip_id, str(trip.service_date), trip.route_id,
                    trip.start_utc.isoformat(), outcome))
    db.commit()
    return results
```

Note the one judgment call baked in and commented: progress in [75%, 90%) with a late last observation classifies COMPLETED (benefit of the doubt) — the spec's VANISHED rule requires *both* low progress and an early cutoff, so this is the spec-faithful residual branch.

- [ ] **Step 4: Verify pass + full suite. Step 5: Commit** — `git add -A && git commit -m "feat: five-class trip outcome classifier with precedence and idempotent day runs"`

---

### Task 5: Aggregates

**Files:**
- Create: `aggregate/rollup.py`, `tests/test_rollup.py`

**Interfaces:**
- Consumes: `trip_outcomes` table (Task 4).
- Produces: `aggregate.rollup.route_day_rollup(db) -> list[dict]` — one dict per (route_id, service_date): `{route_id, service_date, scheduled, excluded, cancelled, completed, vanished, untracked, ghost_rate}` where ghost_rate = (untracked+vanished)/(scheduled−excluded), `None` when the denominator is 0; `aggregate.rollup.route_hour_rollup(db, tz="Europe/Dublin") -> list[dict]` — same counts keyed by (route_id, local_hour of start_utc); both sorted deterministically.

- [ ] **Step 1: Write the failing tests** — `tests/test_rollup.py`:

```python
import datetime as dt
import sqlite3

import pytest

from aggregate.rollup import route_day_rollup, route_hour_rollup

UTC = dt.timezone.utc


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
    CREATE TABLE trip_outcomes (
      trip_id TEXT, service_date TEXT, route_id TEXT, start_utc TEXT, outcome TEXT,
      PRIMARY KEY (trip_id, service_date));
    """)
    rows = [
        ("a", "2026-03-23", "R1", "2026-03-23T07:00:00+00:00", "COMPLETED"),
        ("b", "2026-03-23", "R1", "2026-03-23T07:30:00+00:00", "UNTRACKED"),
        ("c", "2026-03-23", "R1", "2026-03-23T08:00:00+00:00", "VANISHED"),
        ("d", "2026-03-23", "R1", "2026-03-23T08:30:00+00:00", "EXCLUDED"),
        ("e", "2026-03-23", "R1", "2026-03-23T09:00:00+00:00", "CANCELLED"),
        ("f", "2026-03-23", "R2", "2026-03-23T09:00:00+00:00", "EXCLUDED"),
    ]
    conn.executemany("INSERT INTO trip_outcomes VALUES (?,?,?,?,?)", rows)
    conn.commit()
    return conn


def test_route_day_counts_and_ghost_rate(db):
    rollup = route_day_rollup(db)
    r1 = next(r for r in rollup if r["route_id"] == "R1")
    assert r1["scheduled"] == 5 and r1["excluded"] == 1
    assert r1["ghost_rate"] == pytest.approx((1 + 1) / (5 - 1))


def test_all_excluded_route_has_null_rate(db):
    r2 = next(r for r in route_day_rollup(db) if r["route_id"] == "R2")
    assert r2["scheduled"] == 1 and r2["excluded"] == 1 and r2["ghost_rate"] is None


def test_counts_conserve_totals(db):
    for r in route_day_rollup(db):
        parts = r["excluded"] + r["cancelled"] + r["completed"] + r["vanished"] + r["untracked"]
        assert parts == r["scheduled"]


def test_hour_rollup_uses_local_hour(db):
    hours = {(r["route_id"], r["local_hour"]): r for r in route_hour_rollup(db)}
    assert ("R1", 7) in hours and hours[("R1", 7)]["scheduled"] == 2
```

- [ ] **Step 2: failure run**, then **Step 3: Implement** — `aggregate/rollup.py`:

```python
"""Roll trip outcomes up to route/day and route/local-hour tables."""
from __future__ import annotations

import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo

_CLASSES = ("EXCLUDED", "CANCELLED", "COMPLETED", "VANISHED", "UNTRACKED")


def _ghost_rate(counts: dict) -> float | None:
    denom = counts["scheduled"] - counts["excluded"]
    if denom <= 0:
        return None
    return (counts["untracked"] + counts["vanished"]) / denom


def _rollup(rows, key_fn):
    table: dict[tuple, dict] = {}
    for row in rows:
        key = key_fn(row)
        entry = table.setdefault(key, {c.lower(): 0 for c in _CLASSES} | {"scheduled": 0})
        entry["scheduled"] += 1
        entry[row["outcome"].lower()] += 1
    out = []
    for key, counts in sorted(table.items()):
        counts["ghost_rate"] = _ghost_rate(counts)
        out.append(dict(zip(("route_id",) + (("service_date",) if len(key) == 2 and isinstance(key[1], str) else ("local_hour",)), key)) | counts)
    return out


def _fetch(db: sqlite3.Connection):
    cur = db.execute("SELECT trip_id, service_date, route_id, start_utc, outcome FROM trip_outcomes")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def route_day_rollup(db: sqlite3.Connection) -> list[dict]:
    return _rollup(_fetch(db), lambda r: (r["route_id"], r["service_date"]))


def route_hour_rollup(db: sqlite3.Connection, tz: str = "Europe/Dublin") -> list[dict]:
    zone = ZoneInfo(tz)

    def key(r):
        local = dt.datetime.fromisoformat(r["start_utc"]).astimezone(zone)
        return (r["route_id"], local.hour)

    return _rollup(_fetch(db), key)
```

- [ ] **Step 4: Verify pass + full suite. Step 5: Commit** — `git add -A && git commit -m "feat: route/day and route/hour rollups with honest ghost-rate denominator"`

---

### Task 6: Offline-testable GTFS-R poller

**Files:**
- Create: `ingest/poller.py`, `tests/test_poller.py`

**Interfaces:**
- Consumes: `classify.store` (Task 3).
- Produces: `ingest.poller.parse_feed(raw_bytes) -> list[dict]` (decodes a GTFS-R FeedMessage; returns observation dicts `{trip_id, kind, stop_sequence, start_date}` — kind 'cancel' for `schedule_relationship == CANCELED` TripUpdates, 'update' for other TripUpdates using max stop_sequence seen in stop_time_updates, 'position' for VehiclePositions using current_stop_sequence); `ingest.poller.poll_once(db, fetch_fn, now_fn, route_filter: set[str] | None, archive_dir: Path | None) -> int` (calls fetch_fn() → bytes; records heartbeat ok/fail (fetch_fn raising ⇒ heartbeat ok=0, returns -1); parses; records observations (service_date from entity start_date formatted YYYY-MM-DD); optionally zstd-writes raw bytes to `archive_dir/YYYYMMDD/HHMMSS.pb.zst`; returns number of observations recorded); `ingest.poller.run_loop(...)` exists but is a thin uncovered wrapper (`while True: poll_once(...); sleep(60)`) marked `# pragma: no cover`.

- [ ] **Step 1: Write the failing tests** — `tests/test_poller.py` (builds real protobufs with the bindings — no network):

```python
import datetime as dt
import sqlite3
from pathlib import Path

import pytest
from google.transit import gtfs_realtime_pb2 as rt

from classify.store import init_store
from ingest.poller import parse_feed, poll_once

UTC = dt.timezone.utc


def make_feed(entities):
    feed = rt.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = 1774252800
    for e in entities:
        feed.entity.append(e)
    return feed.SerializeToString()


def trip_update(trip_id, start_date="20260323", cancelled=False, max_seq=None):
    e = rt.FeedEntity()
    e.id = f"tu-{trip_id}"
    e.trip_update.trip.trip_id = trip_id
    e.trip_update.trip.start_date = start_date
    if cancelled:
        e.trip_update.trip.schedule_relationship = rt.TripDescriptor.CANCELED
    if max_seq is not None:
        for seq in (1, max_seq):
            stu = e.trip_update.stop_time_update.add()
            stu.stop_sequence = seq
    return e


def vehicle(trip_id, seq, start_date="20260323"):
    e = rt.FeedEntity()
    e.id = f"v-{trip_id}"
    e.vehicle.trip.trip_id = trip_id
    e.vehicle.trip.start_date = start_date
    e.vehicle.current_stop_sequence = seq
    return e


def test_parse_kinds():
    raw = make_feed([trip_update("A", max_seq=4), trip_update("B", cancelled=True),
                     vehicle("C", 2)])
    obs = {o["trip_id"]: o for o in parse_feed(raw)}
    assert obs["A"]["kind"] == "update" and obs["A"]["stop_sequence"] == 4
    assert obs["B"]["kind"] == "cancel"
    assert obs["C"]["kind"] == "position" and obs["C"]["stop_sequence"] == 2
    assert obs["A"]["start_date"] == "20260323"


def test_poll_once_records_heartbeat_and_observations(tmp_path):
    db = sqlite3.connect(":memory:")
    init_store(db)
    raw = make_feed([vehicle("C", 2)])
    now = dt.datetime(2026, 3, 23, 7, 0, tzinfo=UTC)
    n = poll_once(db, fetch_fn=lambda: raw, now_fn=lambda: now,
                  route_filter=None, archive_dir=tmp_path)
    assert n == 1
    assert db.execute("SELECT ok FROM heartbeats").fetchone() == (1,)
    (sd,) = db.execute("SELECT service_date FROM observations").fetchone()
    assert sd == "2026-03-23"
    archived = list(Path(tmp_path).rglob("*.pb.zst"))
    assert len(archived) == 1


def test_fetch_failure_records_bad_heartbeat(tmp_path):
    db = sqlite3.connect(":memory:")
    init_store(db)

    def boom():
        raise ConnectionError("api down")

    now = dt.datetime(2026, 3, 23, 7, 0, tzinfo=UTC)
    n = poll_once(db, fetch_fn=boom, now_fn=lambda: now, route_filter=None, archive_dir=None)
    assert n == -1
    assert db.execute("SELECT ok FROM heartbeats").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM observations").fetchone() == (0,)
```

- [ ] **Step 2: failure run**, then **Step 3: Implement** — `ingest/poller.py`:

```python
"""GTFS-Realtime poller. fetch_fn/now_fn injected so tests never touch the network.

Production wiring (VM): fetch_fn wraps requests.get on the NTA endpoints with the
API key header; run_loop alternates TripUpdates and VehiclePositions at 60 s.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import time
from pathlib import Path
from typing import Callable

import zstandard
from google.transit import gtfs_realtime_pb2 as rt

from classify.store import record_heartbeat, record_observation


def _service_date(start_date: str) -> str:
    return f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}" if len(start_date) == 8 else start_date


def parse_feed(raw: bytes) -> list[dict]:
    feed = rt.FeedMessage()
    feed.ParseFromString(raw)
    out: list[dict] = []
    for entity in feed.entity:
        if entity.HasField("trip_update"):
            tu = entity.trip_update
            if tu.trip.schedule_relationship == rt.TripDescriptor.CANCELED:
                out.append({"trip_id": tu.trip.trip_id, "kind": "cancel",
                            "stop_sequence": None, "start_date": tu.trip.start_date})
            else:
                seqs = [stu.stop_sequence for stu in tu.stop_time_update
                        if stu.HasField("stop_sequence")]
                out.append({"trip_id": tu.trip.trip_id, "kind": "update",
                            "stop_sequence": max(seqs) if seqs else None,
                            "start_date": tu.trip.start_date})
        elif entity.HasField("vehicle"):
            v = entity.vehicle
            out.append({"trip_id": v.trip.trip_id, "kind": "position",
                        "stop_sequence": v.current_stop_sequence or None,
                        "start_date": v.trip.start_date})
    return out


def poll_once(db: sqlite3.Connection, fetch_fn: Callable[[], bytes],
              now_fn: Callable[[], dt.datetime], route_filter: set[str] | None,
              archive_dir: Path | None) -> int:
    now = now_fn()
    try:
        raw = fetch_fn()
    except Exception:
        record_heartbeat(db, now.isoformat(), False)
        return -1
    record_heartbeat(db, now.isoformat(), True)
    if archive_dir is not None:
        day_dir = Path(archive_dir) / now.strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / f"{now.strftime('%H%M%S')}.pb.zst").write_bytes(
            zstandard.ZstdCompressor().compress(raw))
    count = 0
    for obs in parse_feed(raw):
        if not obs["trip_id"]:
            continue
        if route_filter is not None and obs.get("route_id") not in route_filter:
            pass  # route filtering happens at classify time via the timetable join
        record_observation(db, obs["trip_id"], _service_date(obs["start_date"]),
                           now.isoformat(), obs["kind"], obs["stop_sequence"])
        count += 1
    return count


def run_loop(db, fetch_fns: list[Callable[[], bytes]], archive_dir: Path,
             interval_s: int = 60) -> None:  # pragma: no cover
    i = 0
    while True:
        poll_once(db, fetch_fns[i % len(fetch_fns)],
                  lambda: dt.datetime.now(dt.timezone.utc), None, archive_dir)
        i += 1
        time.sleep(interval_s)
```

- [ ] **Step 4: Verify pass + full suite. Step 5: Commit** — `git add -A && git commit -m "feat: offline-testable GTFS-R poller with heartbeats and zstd archive"`

---

### Task 7: Publish gate + ops files + README seed

**Files:**
- Create: `run_checks.py`, `tests/test_checks.py`, `ops/ghostbus-poller.service`, `ops/ghostbus-classifier.service`, `ops/ghostbus-classifier.timer`, `ops/RUNBOOK.md`, `README.md`

**Interfaces:**
- Consumes: rollup functions (Task 5), `trip_outcomes` (Task 4).
- Produces: `run_checks.py` with `check_conservation(db) -> dict` (per route/day: class counts sum to scheduled), `check_rates_bounded(db) -> dict` (every non-null ghost_rate ∈ [0,1]), `check_outcomes_valid(db) -> dict` (every outcome ∈ OUTCOMES); `main()` runs all, prints PASS/FAIL lines, exits 1 on failure.

- [ ] **Step 1: Write the failing test** — `tests/test_checks.py`:

```python
import sqlite3
import subprocess
import sys

from run_checks import check_conservation, check_outcomes_valid, check_rates_bounded


def make_db(rows):
    db = sqlite3.connect(":memory:")
    db.executescript("""
    CREATE TABLE trip_outcomes (
      trip_id TEXT, service_date TEXT, route_id TEXT, start_utc TEXT, outcome TEXT,
      PRIMARY KEY (trip_id, service_date));
    """)
    db.executemany("INSERT INTO trip_outcomes VALUES (?,?,?,?,?)", rows)
    db.commit()
    return db


GOOD = [("a", "2026-03-23", "R1", "2026-03-23T07:00:00+00:00", "COMPLETED"),
        ("b", "2026-03-23", "R1", "2026-03-23T07:30:00+00:00", "UNTRACKED")]


def test_all_checks_pass_on_good_db():
    db = make_db(GOOD)
    assert check_conservation(db)["passed"]
    assert check_rates_bounded(db)["passed"]
    assert check_outcomes_valid(db)["passed"]


def test_invalid_outcome_fails():
    db = make_db(GOOD + [("z", "2026-03-23", "R1", "2026-03-23T08:00:00+00:00", "MAYBE")])
    assert not check_outcomes_valid(db)["passed"]


def test_cli_exit_codes(tmp_path):
    # empty db file -> checks run on empty tables -> pass, exit 0
    dbfile = tmp_path / "s.db"
    db = sqlite3.connect(dbfile)
    db.executescript("""
    CREATE TABLE trip_outcomes (
      trip_id TEXT, service_date TEXT, route_id TEXT, start_utc TEXT, outcome TEXT,
      PRIMARY KEY (trip_id, service_date));
    """)
    db.commit(); db.close()
    proc = subprocess.run([sys.executable, "run_checks.py", str(dbfile)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
```

- [ ] **Step 2: failure run**, then **Step 3: Implement** — `run_checks.py`:

```python
"""Publish gate: the site never ships numbers these checks didn't pass."""
from __future__ import annotations

import sqlite3
import sys

from aggregate.rollup import route_day_rollup
from classify.outcomes import OUTCOMES


def check_conservation(db: sqlite3.Connection) -> dict:
    bad = []
    for r in route_day_rollup(db):
        parts = r["excluded"] + r["cancelled"] + r["completed"] + r["vanished"] + r["untracked"]
        if parts != r["scheduled"]:
            bad.append(r)
    return {"check": "conservation", "passed": not bad, "violations": bad}


def check_rates_bounded(db: sqlite3.Connection) -> dict:
    bad = [r for r in route_day_rollup(db)
           if r["ghost_rate"] is not None and not 0.0 <= r["ghost_rate"] <= 1.0]
    return {"check": "rates_bounded", "passed": not bad, "violations": bad}


def check_outcomes_valid(db: sqlite3.Connection) -> dict:
    marks = ",".join("?" * len(OUTCOMES))
    bad = db.execute(
        f"SELECT trip_id, outcome FROM trip_outcomes WHERE outcome NOT IN ({marks})",
        OUTCOMES).fetchall()
    return {"check": "outcomes_valid", "passed": not bad, "violations": bad}


def main() -> int:
    db = sqlite3.connect(sys.argv[1] if len(sys.argv) > 1 else "state/ghostbus.db")
    results = [check_conservation(db), check_rates_bounded(db), check_outcomes_valid(db)]
    for r in results:
        print(("PASS" if r["passed"] else "FAIL"), r["check"])
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: ops files** (text artifacts; content complete, deployment happens in Phase 2):

`ops/ghostbus-poller.service`:
```ini
[Unit]
Description=Ghost Bus GTFS-R poller
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/ghost-bus
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/etc/ghostbus.env
ExecStart=/opt/ghost-bus/.venv/bin/python -m ingest.run_poller
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
`ops/ghostbus-classifier.service`:
```ini
[Unit]
Description=Ghost Bus classifier pass

[Service]
Type=oneshot
WorkingDirectory=/opt/ghost-bus
EnvironmentFile=/etc/ghostbus.env
ExecStart=/opt/ghost-bus/.venv/bin/python -m classify.run_classifier
```
`ops/ghostbus-classifier.timer`:
```ini
[Unit]
Description=Run the Ghost Bus classifier every 10 minutes

[Timer]
OnCalendar=*:0/10
Persistent=true

[Install]
WantedBy=timers.target
```
`ops/RUNBOOK.md`: sections — Provisioning (Oracle free-tier VM checklist, referencing the two account tasks that only the owner performs: NTA key at developer.nationaltransport.ie, VM creation), Install (`git clone` → venv → `pip install -r requirements.txt` → copy systemd units → `/etc/ghostbus.env` holds `NTA_API_KEY`), Health (heartbeat query one-liner, healthchecks.io ping wiring), Recovery (poller restart, timetable refresh, disk cleanup of archives > 7 days). Write it complete — every command spelled out, no placeholders except the literal API key value.

- [ ] **Step 5: README.md seed** — hero ("Which Dublin buses actually show up? A 24/7 tracker that measures ghost buses — honestly."), status badge, the five-class taxonomy table with the UNTRACKED caveat, "the tracker grades itself" paragraph (EXCLUDED + uptime strip), quick start (pytest), phase status (core complete; live deployment pending API key + VM), spec/plan links, NTA attribution + "not affiliated with TFI/NTA".

- [ ] **Step 6: Verify** — full suite `python -m pytest` green; `python run_checks.py` against a scratch db exits 0.
- [ ] **Step 7: Commit** — `git add -A && git commit -m "feat: publish gate, systemd units, runbook, README"`

---

## Self-Review (done at write time)

- **Spec coverage (this phase):** taxonomy+precedence ✓(T4, thresholds verbatim incl. the documented residual branch) noon-rule/DST/past-24h ✓(T2, tested on 2026-03-29) uptime→EXCLUDED ✓(T3,T4) idempotent classification ✓(T4) rollups + honest denominator ✓(T5) publish gate ✓(T7) poller heartbeats/archive/injected-fetch ✓(T6) ops units/runbook ✓(T7) CI no-network ✓(T1). Deferred to the Phase-2 plan by design: `ingest.run_poller`/`classify.run_classifier` production entry-points (referenced by systemd units, written during deployment when real endpoints/env exist), real-GTFS download, publisher/site.
- **Type consistency:** `ScheduledTrip` fields used by T4 match T2's dataclass; store schema used by T4/T6 matches T3; `trip_outcomes` columns identical in T4/T5/T7 test DDL; outcome strings identical everywhere.
- **Placeholder scan:** none; the two runtime entry-point modules are explicitly declared Phase-2 deliverables, not silently missing.
