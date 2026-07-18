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
    # Distinct minute buckets, not raw heartbeat rows - a crash-loop or sub-minute
    # retry storm must not inflate uptime by counting the same minute repeatedly.
    (got,) = db.execute(
        "SELECT COUNT(DISTINCT substr(ts_utc,1,16)) FROM heartbeats "
        "WHERE ok=1 AND ts_utc>=? AND ts_utc<?",
        (start_utc.isoformat(), end_utc.isoformat())).fetchone()
    return min(1.0, got / expected)
