"""Trip outcome classification — the spec's taxonomy, precedence order, first match wins.

EXCLUDED   tracker uptime < 90% of the trip window (our fault, not the operator's)
CANCELLED  feed marked the trip CANCELED during the window
COMPLETED  progress >= 90% of stops OR last observation within 10 min of scheduled end
VANISHED   observed, then silent with progress < 75% and > 15 min still to run
UNTRACKED  no VEHICLE observation in the window (TripUpdate predictions alone do
           not prove a bus exists - predictions-without-a-vehicle is exactly the
           commuter's ghost)
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
    tracked = [(ts, seq) for ts, kind, seq in rows if kind == "position"]
    if not tracked:
        return "UNTRACKED"
    # Parse before comparing - string order breaks if timestamp formats ever vary.
    last_ts = max(dt.datetime.fromisoformat(ts) for ts, _ in tracked)
    seqs = [seq for _, seq in tracked if seq is not None]
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
