"""Trip outcome classification — the spec's taxonomy, precedence order, first match wins.

EXCLUDED   tracker uptime < 90% of the trip window (our fault, not the operator's)
CANCELLED  feed marked the trip CANCELED during the window
COMPLETED  progress >= 90% of stops OR last evidence within 10 min of scheduled end
           (progress = feed stop_sequence UNION geographic nearest-stop matching, G1)
VANISHED   observed, then silent with progress < 75% and > 15 min still to run
           (progress = feed stop_sequence UNION geographic nearest-stop matching, G1)
UNTRACKED  no VEHICLE observation in the window (TripUpdate predictions alone do
           not prove a bus exists - predictions-without-a-vehicle is exactly the
           commuter's ghost)
EXCLUDED_FEED  (amendment G3, 2026-07-22) the trip would classify VANISHED or
           UNTRACKED, but the FEED itself was degraded for this operator over
           the trip's window (classify/feedhealth.py): position volume
           collapsed at fleet scale, so trip-level silence is not evidence
           about this bus. Not operator blame, not tracker downtime - NTA's
           failure, named as such. The shield only ever converts the two
           accusatory classes; COMPLETED and CANCELLED are never touched
           (evidence that exists still counts), and EXCLUDED takes precedence
           (our downtime is reported as ours).

Amendment G2 (2026-07-22): a position ping is evidence of the moment the
VEHICLE reported (vehicle_ts), not the moment we fetched it (ts_utc) - the
NTA feed republishes stale positions (2.3-2.5% of pings arrive already older
than the 10-minute credit window, measured over the burn-in baseline), and
crediting fetch time let a bus that went silent stay "alive" on republished
evidence. Evidence time = min(vehicle_ts, ts_utc): NULL vehicle_ts falls
back to ts_utc (pre-migration rows reproduce pre-G2 behaviour exactly), and
the min() means a vehicle clock running ahead of ours can never extend
credit - G2 is monotonically stricter, so no trip can become COMPLETED
under it that was not already. Window membership and the UNTRACKED
existence test deliberately stay on ts_utc: reinterpreting clocks may only
remove operator-flattering credit, never manufacture the accusatory class.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

from classify.progress import matched_max_seq
from classify.store import uptime
from timetable.gtfs import ScheduledTrip

OUTCOMES = ("EXCLUDED", "CANCELLED", "COMPLETED", "VANISHED", "UNTRACKED",
            "EXCLUDED_FEED")

_OUTCOMES_SCHEMA = """
CREATE TABLE IF NOT EXISTS trip_outcomes (
  trip_id TEXT, service_date TEXT, route_id TEXT, start_utc TEXT, outcome TEXT,
  PRIMARY KEY (trip_id, service_date));
"""


def _trip_stop_coords(db: sqlite3.Connection, trip_id: str) -> list[tuple[int, float, float]]:
    try:
        return db.execute(
            "SELECT st.stop_sequence, s.lat, s.lon FROM gtfs_stop_times st "
            "JOIN gtfs_stops s ON s.stop_id = st.stop_id WHERE st.trip_id=?",
            (trip_id,)).fetchall()
    except sqlite3.OperationalError as exc:
        # Pre-refresh database (no gtfs_stops table / stop_id column yet):
        # geographic evidence is simply unavailable - progress falls back to
        # feed stop_sequence alone, i.e. pre-G1 behavior. Anything else
        # (I/O error, corruption) must crash: silently dropping geo evidence
        # would shift outcomes toward VANISHED against the published method.
        if "no such table" in str(exc) or "no such column" in str(exc):
            return []
        raise


def _feed_degraded(trip: ScheduledTrip, shields: dict | None,
                   agency_of_route: dict | None) -> bool:
    """Does any degraded interval for this trip's operator overlap its window?"""
    if not shields or not agency_of_route:
        return False
    intervals = shields.get(agency_of_route.get(trip.route_id), ())
    return any(start < trip.window_end_utc and end > trip.window_start_utc
               for start, end in intervals)


def classify_trip(db: sqlite3.Connection, trip: ScheduledTrip,
                  radius_m: float = 250.0,
                  shields: dict | None = None,
                  agency_of_route: dict | None = None) -> str:
    if uptime(db, trip.window_start_utc, trip.window_end_utc) < 0.90:
        return "EXCLUDED"
    rows = db.execute(
        "SELECT ts_utc, kind, stop_sequence, lat, lon, vehicle_ts FROM observations "
        "WHERE trip_id=? AND service_date=? AND ts_utc>=? AND ts_utc<? ORDER BY ts_utc",
        (trip.trip_id, str(trip.service_date),
         trip.window_start_utc.isoformat(), trip.window_end_utc.isoformat())).fetchall()
    if any(kind == "cancel" for _, kind, _, _, _, _ in rows):
        return "CANCELLED"
    tracked = [(ts, seq, lat, lon, vts)
               for ts, kind, seq, lat, lon, vts in rows if kind == "position"]
    # Amendment G3: the shield converts ONLY the two accusatory classes, at
    # their return points - a degraded feed makes trip-level silence
    # unusable as evidence against the operator, but evidence that exists
    # (COMPLETED, CANCELLED) still counts, and EXCLUDED has already returned
    # above (our downtime is reported as ours, never as NTA's).
    if not tracked:
        return "EXCLUDED_FEED" if _feed_degraded(trip, shields, agency_of_route) \
            else "UNTRACKED"
    # Evidence clock (amendment G2): min(vehicle_ts, ts_utc), ts_utc when
    # vehicle_ts is NULL. Parse before comparing - string order breaks if
    # timestamp formats ever vary. A malformed vehicle_ts raises: the poller
    # pins ISO-or-NULL at ingest, so anything else is database corruption and
    # must crash rather than silently reshape outcomes (same rule as the geo
    # query below).
    def evidence_ts(ts: str, vts: str | None) -> dt.datetime:
        fetched = dt.datetime.fromisoformat(ts)
        return fetched if vts is None else min(dt.datetime.fromisoformat(vts), fetched)

    last_ts = max(evidence_ts(ts, vts) for ts, _, _, _, vts in tracked)
    seqs = [seq for _, seq, _, _, _ in tracked if seq is not None]
    # Geographic evidence (amendment G1): GPS pings matched to the trip's own
    # scheduled stops. Merges with feed stop_sequence by max - it can only
    # RAISE progress, never lower it or affect any other class.
    # Only pings whose EVIDENCE time (G2) is at/after the scheduled start
    # carry progress: a vehicle keyed to the trip during the 5-min pre-window
    # (layover near a terminus), or a post-start republication of such a
    # pre-start position, must not complete a trip that never departed.
    # Existence still counts above.
    pings = [(lat, lon) for ts, _, lat, lon, vts in tracked
             if lat is not None and lon is not None
             and evidence_ts(ts, vts) >= trip.start_utc]
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
        return "EXCLUDED_FEED" if _feed_degraded(trip, shields, agency_of_route) \
            else "VANISHED"
    # Residual: neither clearly completed nor vanished (incl. any-progress trips last
    # seen 10-15 min before scheduled end) - benefit of the doubt goes to the operator.
    return "COMPLETED"


def classify_day(db: sqlite3.Connection, trips: list[ScheduledTrip],
                 now_utc: dt.datetime, radius_m: float = 250.0,
                 shields: dict | None = None,
                 agency_of_route: dict | None = None) -> dict[str, str]:
    db.executescript(_OUTCOMES_SCHEMA)
    results: dict[str, str] = {}
    rows: list[tuple] = []
    # Classify first, write after: classify_trip is minutes of pure-read compute
    # at day scale, and an open write transaction across it would starve the
    # live poller (busy_timeout 30 s) on the shared SQLite file.
    for trip in trips:
        if trip.window_end_utc > now_utc:
            continue
        outcome = classify_trip(db, trip, radius_m, shields, agency_of_route)
        results[trip.trip_id] = outcome
        rows.append((trip.trip_id, str(trip.service_date), trip.route_id,
                     trip.start_utc.isoformat(), outcome))
    db.executemany("INSERT OR REPLACE INTO trip_outcomes VALUES (?,?,?,?,?)", rows)
    db.commit()
    return results
