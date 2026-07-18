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
    # Compute naive local time: service date noon minus 12h plus seconds
    naive_start = dt.datetime(service_date.year, service_date.month, service_date.day, 12, 0)
    naive_result = naive_start - dt.timedelta(hours=12) + dt.timedelta(seconds=seconds)

    # Create aware datetime
    result_local = naive_result.replace(tzinfo=zone, fold=0)
    current_offset = result_local.utcoffset()

    # Check for spring-forward DST transitions by looking ahead incrementally
    check_time = result_local
    for _ in range(25):  # Check up to 24 hours ahead
        next_check = check_time + dt.timedelta(hours=1)
        if next_check.utcoffset() > current_offset:
            # Found a spring-forward! Add the offset delta
            offset_delta = next_check.utcoffset() - current_offset
            result_utc = result_local.astimezone(UTC) + offset_delta
            result_local = result_utc.astimezone(zone)
            break
        check_time = next_check

    return result_local


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
