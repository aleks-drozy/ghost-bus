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
CREATE TABLE IF NOT EXISTS gtfs_calendar_dates (
  service_id TEXT, date TEXT, exception_type INTEGER);
CREATE TABLE IF NOT EXISTS gtfs_routes (route_id TEXT PRIMARY KEY, agency_id TEXT);
CREATE TABLE IF NOT EXISTS gtfs_agency (agency_id TEXT PRIMARY KEY, agency_name TEXT);
CREATE TABLE IF NOT EXISTS gtfs_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_stop_times_trip ON gtfs_stop_times(trip_id);
"""


def gtfs_seconds(hms: str) -> int:
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def local_from_service(service_date: dt.date, seconds: int, tz: str) -> dt.datetime:
    """GTFS time = real ELAPSED seconds after (service-day noon local - 12 h).

    Arithmetic happens in UTC: aware-datetime arithmetic within one tzinfo is
    wall-clock in Python, which is not what GTFS specifies - doing it in UTC
    makes spring-forward gaps and fall-back folds impossible to hit.
    """
    zone = ZoneInfo(tz)
    noon = dt.datetime(service_date.year, service_date.month, service_date.day,
                       12, 0, tzinfo=zone)
    base_utc = noon.astimezone(UTC) - dt.timedelta(hours=12)
    return (base_utc + dt.timedelta(seconds=seconds)).astimezone(zone)


_INSERT_BATCH = 50_000  # stream inserts: a full national stop_times list OOMs a 1 GB VM


def _insert_stream(db: sqlite3.Connection, sql: str, tuples) -> None:
    batch = []
    for row in tuples:
        batch.append(row)
        if len(batch) >= _INSERT_BATCH:
            db.executemany(sql, batch)
            batch.clear()
    if batch:
        db.executemany(sql, batch)


def load_gtfs(zip_path: str | Path, db: sqlite3.Connection) -> str:
    digest = hashlib.sha256(Path(zip_path).read_bytes()).hexdigest()
    db.executescript(_SCHEMA)
    with zipfile.ZipFile(zip_path) as zf:
        def rows(name):
            with zf.open(name) as fh:
                yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))

        db.execute("DELETE FROM gtfs_trips"); db.execute("DELETE FROM gtfs_stop_times")
        db.execute("DELETE FROM gtfs_calendar")
        db.execute("DELETE FROM gtfs_calendar_dates")
        db.execute("DELETE FROM gtfs_routes")
        db.execute("DELETE FROM gtfs_agency")
        _insert_stream(db, "INSERT INTO gtfs_trips VALUES (?,?,?)",
                       ((r["trip_id"], r["route_id"], r["service_id"]) for r in rows("trips.txt")))
        _insert_stream(db, "INSERT INTO gtfs_routes VALUES (?,?)",
                       ((r["route_id"], r["agency_id"]) for r in rows("routes.txt")))
        _insert_stream(db, "INSERT INTO gtfs_agency VALUES (?,?)",
                       ((r["agency_id"], r["agency_name"]) for r in rows("agency.txt")))
        _insert_stream(db, "INSERT INTO gtfs_stop_times VALUES (?,?,?)",
                       ((r["trip_id"], int(r["stop_sequence"]), gtfs_seconds(r["departure_time"]))
                        for r in rows("stop_times.txt")))
        _insert_stream(db, "INSERT INTO gtfs_calendar VALUES (?,?,?,?,?,?,?,?,?,?)",
                       ((r["service_id"], *[int(r[d]) for d in
                         ("monday", "tuesday", "wednesday", "thursday", "friday",
                          "saturday", "sunday")], r["start_date"], r["end_date"])
                        for r in rows("calendar.txt")))
        if "calendar_dates.txt" in zf.namelist():
            _insert_stream(db, "INSERT INTO gtfs_calendar_dates VALUES (?,?,?)",
                           ((r["service_id"], r["date"], int(r["exception_type"]))
                            for r in rows("calendar_dates.txt")))
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
    max_stop_seq: int


_WEEKDAY_COLS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def scheduled_trips(db: sqlite3.Connection, service_date: dt.date,
                    tz: str = "Europe/Dublin", *,
                    agency_names: set[str] | None = None) -> list[ScheduledTrip]:
    datestr = service_date.strftime("%Y%m%d")
    col = _WEEKDAY_COLS[service_date.weekday()]
    services = {sid for (sid,) in db.execute(
        f"SELECT service_id FROM gtfs_calendar WHERE {col}=1 AND start_date<=? AND end_date>=?",
        (datestr, datestr))}
    # calendar_dates.txt exceptions override the weekly pattern for this exact date:
    # type 1 adds a service running that day, type 2 removes one. Applied before the
    # early-out so an all-exceptions feed (no calendar.txt matches at all) still works.
    for sid, exc_type in db.execute(
            "SELECT service_id, exception_type FROM gtfs_calendar_dates WHERE date=?",
            (datestr,)):
        if exc_type == 1:
            services.add(sid)
        elif exc_type == 2:
            services.discard(sid)
    if not services:
        return []
    if agency_names is not None and not agency_names:
        return []
    out: list[ScheduledTrip] = []
    marks = ",".join("?" * len(services))
    if agency_names is None:
        query = f"SELECT trip_id, route_id FROM gtfs_trips WHERE service_id IN ({marks})"
        params: tuple = tuple(services)
    else:
        # Scope to agency_names via trips -> routes -> agency; unmatched names
        # simply yield no rows rather than erroring.
        agency_marks = ",".join("?" * len(agency_names))
        query = (
            "SELECT t.trip_id, t.route_id FROM gtfs_trips t "
            "JOIN gtfs_routes r ON r.route_id = t.route_id "
            "JOIN gtfs_agency a ON a.agency_id = r.agency_id "
            f"WHERE t.service_id IN ({marks}) AND a.agency_name IN ({agency_marks})"
        )
        params = tuple(services) + tuple(agency_names)
    for trip_id, route_id in db.execute(query, params):
        row = db.execute(
            "SELECT MIN(dep_seconds), MAX(dep_seconds), COUNT(*), MAX(stop_sequence) "
            "FROM gtfs_stop_times WHERE trip_id=?",
            (trip_id,)).fetchone()
        first_s, last_s, n_stops, max_stop_seq = row
        start = local_from_service(service_date, first_s, tz).astimezone(UTC)
        end = local_from_service(service_date, last_s, tz).astimezone(UTC)
        out.append(ScheduledTrip(
            trip_id, route_id, service_date, start, end,
            start - dt.timedelta(minutes=5), end + dt.timedelta(minutes=15), n_stops, max_stop_seq))
    out.sort(key=lambda t: (t.start_utc, t.trip_id))
    return out
