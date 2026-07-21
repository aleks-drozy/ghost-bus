"""Shared synthetic database for publish/dataset.py tests.

Offline and in-memory. Deliberately shaped to exercise every published edge:
a normally-populated route, a route whose denominator is zero (all EXCLUDED),
and a production-shaped route_id containing spaces that is absent from
gtfs_routes.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

# A Monday, six days before the 2026-03-29 DST change, so Europe/Dublin is
# UTC+0 that day and the golden files stay readable.
SERVICE_DATE = "2026-03-23"

# sha256 of the empty byte string: a fixed, obviously-synthetic 64-hex digest.
GTFS_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
GTFS_LOADED_AT = "2026-03-01T02:00:00+00:00"

_SCHEMA = """
CREATE TABLE trip_outcomes (
  trip_id TEXT, service_date TEXT, route_id TEXT, start_utc TEXT, outcome TEXT,
  PRIMARY KEY (trip_id, service_date));
CREATE TABLE heartbeats (ts_utc TEXT PRIMARY KEY, ok INTEGER);
CREATE TABLE observations (
  trip_id TEXT, service_date TEXT, ts_utc TEXT, kind TEXT, stop_sequence INTEGER,
  lat REAL, lon REAL, vehicle_ts TEXT);
CREATE TABLE gtfs_routes (route_id TEXT PRIMARY KEY, agency_id TEXT,
  route_short_name TEXT, route_long_name TEXT);
CREATE TABLE gtfs_agency (agency_id TEXT PRIMARY KEY, agency_name TEXT);
CREATE TABLE gtfs_meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Two ok polls land in the same minute bucket (00:00) - a retry storm must not
# inflate uptime. One failed poll is not an ok minute. Net: 2 ok minutes.
HEARTBEATS = [
    ("2026-03-23T00:00:00.100000+00:00", 1),
    ("2026-03-23T00:00:30.100000+00:00", 1),
    ("2026-03-23T00:01:00.100000+00:00", 1),
    ("2026-03-23T00:02:00.100000+00:00", 0),
]

OBSERVATIONS = [
    ("R1_00_2026-03-23", SERVICE_DATE, "2026-03-23T07:01:00+00:00", "position",
     1, 53.3000, -6.2000, None),
    ("R1_00_2026-03-23", SERVICE_DATE, "2026-03-23T07:15:00+00:00", "position",
     3, 53.3072, -6.2000, None),
    ("R1_03_2026-03-23", SERVICE_DATE, "2026-03-23T10:05:00+00:00", "update",
     2, None, None, None),
]

GTFS_ROUTES = [
    ("R1", "FVB", "1", "Fixtureville Main"),
    ("R2", "FVB", "2", "Fixtureville Orbital"),
]
GTFS_AGENCY = [("FVB", "Fixtureville Bus")]

# Absent from GTFS_ROUTES on purpose: production route ids look like this and
# must surface in manifest.unnamed_routes rather than being dropped.
UNNAMED_ROUTE_ID = "03C 120 e a"


def outcome_rows(service_date: str = SERVICE_DATE) -> list[tuple]:
    """R1: 10 scheduled / 2 excluded -> denominator 8, 1 vanished, 1 untracked.
    R2: 1 scheduled / 1 excluded -> denominator 0, both rates undefined.
    UNNAMED_ROUTE_ID: 2 scheduled, 1 completed, 1 vanished."""
    kinds = (["EXCLUDED"] * 2 + ["CANCELLED"] + ["COMPLETED"] * 5
             + ["VANISHED"] + ["UNTRACKED"])
    rows = [(f"R1_{i:02d}_{service_date}", service_date, "R1",
             f"{service_date}T{7 + i:02d}:00:00+00:00", kind)
            for i, kind in enumerate(kinds)]
    rows.append((f"R2_00_{service_date}", service_date, "R2",
                 f"{service_date}T09:00:00+00:00", "EXCLUDED"))
    rows.append((f"U_00_{service_date}", service_date, UNNAMED_ROUTE_ID,
                 f"{service_date}T10:00:00+00:00", "COMPLETED"))
    rows.append((f"U_01_{service_date}", service_date, UNNAMED_ROUTE_ID,
                 f"{service_date}T11:00:00+00:00", "VANISHED"))
    return rows


def consecutive_dates(n: int, start: str = "2026-03-02") -> list[str]:
    d0 = dt.date.fromisoformat(start)
    return [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n)]


def build_db(service_dates=(SERVICE_DATE,), heartbeats=None) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(_SCHEMA)
    for day in service_dates:
        db.executemany("INSERT INTO trip_outcomes VALUES (?,?,?,?,?)",
                       outcome_rows(day))
    db.executemany("INSERT INTO heartbeats VALUES (?,?)",
                   HEARTBEATS if heartbeats is None else heartbeats)
    db.executemany("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)", OBSERVATIONS)
    db.executemany("INSERT INTO gtfs_routes VALUES (?,?,?,?)", GTFS_ROUTES)
    db.executemany("INSERT INTO gtfs_agency VALUES (?,?)", GTFS_AGENCY)
    db.execute("INSERT INTO gtfs_meta VALUES ('gtfs_hash', ?)", (GTFS_HASH,))
    db.execute("INSERT INTO gtfs_meta VALUES ('gtfs_loaded_at', ?)", (GTFS_LOADED_AT,))
    db.commit()
    return db
