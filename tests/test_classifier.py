import datetime as dt
import sqlite3

import pytest

from classify.outcomes import OUTCOMES, classify_day, classify_trip
from classify.store import init_store, record_heartbeat, record_observation
from timetable.gtfs import ScheduledTrip

UTC = dt.timezone.utc
DAY = dt.date(2026, 3, 23)


def make_trip(trip_id="T1", start_h=7, dur_min=60, n_stops=5, max_stop_seq=None):
    max_stop_seq = n_stops if max_stop_seq is None else max_stop_seq
    start = dt.datetime(2026, 3, 23, start_h, 0, tzinfo=UTC)
    end = start + dt.timedelta(minutes=dur_min)
    return ScheduledTrip(trip_id, "R1", DAY, start, end,
                         start - dt.timedelta(minutes=5), end + dt.timedelta(minutes=15),
                         n_stops, max_stop_seq)


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


def test_predictions_alone_do_not_count_as_tracking(db):
    # A trip with only TripUpdate predictions (kind='update') and no vehicle
    # positions is UNTRACKED: predictions are what the app shows for a ghost.
    trip = make_trip()
    beat_window(db, trip)
    record_observation(db, trip.trip_id, str(DAY),
                       (trip.start_utc + dt.timedelta(minutes=10)).isoformat(), "update", 5)
    record_observation(db, trip.trip_id, str(DAY),
                       (trip.end_utc - dt.timedelta(minutes=5)).isoformat(), "update", 5)
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

    # Same monotonicity, but starting from a good outcome: downtime overrides
    # COMPLETED too, not just UNTRACKED.
    trip2 = make_trip("T2")
    beat_window(db, trip2)
    obs(db, trip2, 55, 5)
    assert classify_trip(db, trip2) == "COMPLETED"
    db.execute("DELETE FROM heartbeats")
    assert classify_trip(db, trip2) == "EXCLUDED"


def test_progress_with_non_contiguous_stop_sequences(db):
    # Real feeds number stops 10,20,...,50: max seq 50, 5 stops.
    trip = make_trip(n_stops=5, max_stop_seq=50)
    beat_window(db, trip)
    obs(db, trip, 30, 20)  # stop 2 of 5 -> progress 0.4, mid-route, early cutoff
    assert classify_trip(db, trip) == "VANISHED"
    trip2 = make_trip("T2", n_stops=5, max_stop_seq=50)
    beat_window(db, trip2)
    obs(db, trip2, 55, 50)  # final stop -> progress 1.0
    assert classify_trip(db, trip2) == "COMPLETED"


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
