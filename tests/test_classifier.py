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
