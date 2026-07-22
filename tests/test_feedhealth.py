"""Amendment G3: per-operator feed-health detection (schedule-relative).

reporting_fraction = scheduled trips active in a 10-min bucket with >=1
position ping / scheduled trips active. A bucket is degraded when the
fraction drops below THRESHOLD with at least MIN_ACTIVE_TRIPS active and
the tracker itself watching; only runs of >= MIN_RUN consecutive degraded
buckets arm the gate.
"""
import datetime as dt
import sqlite3

import pytest

from classify.feedhealth import (BUCKET_S, MIN_ACTIVE_TRIPS, MIN_RUN,
                                 THRESHOLD, bucket_index, compute_shields,
                                 find_degraded_runs, route_agency_map)
from classify.store import init_store, record_heartbeat, record_observation
from timetable.gtfs import ScheduledTrip

UTC = dt.timezone.utc
DAY = dt.date(2026, 3, 23)
T0 = dt.datetime(2026, 3, 23, 12, 0, tzinfo=UTC)  # bucket-aligned (12:00 UTC)


# ---- pure core -------------------------------------------------------------

def test_find_degraded_runs_flags_a_sustained_collapse():
    active = {b: 100 for b in range(10, 20)}
    reporting = {b: 90 for b in range(10, 20)}
    reporting[14] = 20
    reporting[15] = 25
    reporting[16] = 30
    assert find_degraded_runs(active, reporting, unwatched=set()) == [(14, 17)]


def test_find_degraded_runs_ignores_a_single_noisy_bucket():
    # MIN_RUN=2: one bad bucket alone must not blank an interval.
    active = {b: 100 for b in range(10, 20)}
    reporting = {b: 90 for b in range(10, 20)}
    reporting[14] = 10
    assert find_degraded_runs(active, reporting, unwatched=set()) == []


def test_find_degraded_runs_needs_minimum_active_trips():
    # Overnight noise: 3 active trips, none reporting - fraction 0.0, but the
    # sample is far too small to accuse the feed. Never evaluated.
    active = {b: MIN_ACTIVE_TRIPS - 1 for b in range(10, 20)}
    reporting = {b: 0 for b in range(10, 20)}
    assert find_degraded_runs(active, reporting, unwatched=set()) == []


def test_find_degraded_runs_skips_unwatched_buckets():
    # Our own downtime must not read as feed degradation - those trips are
    # EXCLUDED's job. An unwatched bucket is not evaluated, and it splits a
    # run rather than joining two half-runs into one armed interval.
    active = {b: 100 for b in range(10, 20)}
    reporting = dict.fromkeys(range(10, 20), 0)
    assert find_degraded_runs(active, reporting, unwatched=set(range(10, 20))) == []
    # split: degraded at 12,13 and 15,16 with 14 unwatched -> two runs
    reporting2 = {b: 90 for b in range(10, 20)}
    for b in (12, 13, 15, 16):
        reporting2[b] = 0
    assert find_degraded_runs(active, reporting2, unwatched={14}) == [(12, 14), (15, 17)]


def test_find_degraded_runs_disjoint_runs_stay_separate():
    active = {b: 100 for b in range(0, 30)}
    reporting = {b: 90 for b in range(0, 30)}
    for b in (5, 6, 20, 21, 22):
        reporting[b] = 0
    assert find_degraded_runs(active, reporting, unwatched=set()) == [(5, 7), (20, 23)]


# ---- integration: compute_shields ------------------------------------------

def make_trip(trip_id, route_id="R1", start_min=0, dur_min=60):
    start = T0 + dt.timedelta(minutes=start_min)
    end = start + dt.timedelta(minutes=dur_min)
    return ScheduledTrip(trip_id, route_id, DAY, start, end,
                         start - dt.timedelta(minutes=5),
                         end + dt.timedelta(minutes=15), 5, 5)


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    init_store(conn)
    return conn


def beat_span(db, start, end):
    t = start
    while t < end:
        record_heartbeat(db, t.isoformat(), True)
        t += dt.timedelta(minutes=1)


def ping(db, trip, minutes_after_start):
    record_observation(db, trip.trip_id, str(DAY),
                       (trip.start_utc + dt.timedelta(minutes=minutes_after_start)).isoformat(),
                       "position")
    # feedhealth counts a trip toward every bucket it pinged in


def _fleet(n, route="R1", start_min=0, dur_min=60):
    return [make_trip(f"T{route}{i}", route, start_min, dur_min) for i in range(n)]


AGENCY = {"R1": "OpA", "R2": "OpB"}


def test_shields_empty_when_everyone_reports(db):
    trips = _fleet(40)
    beat_span(db, T0 - dt.timedelta(minutes=10), T0 + dt.timedelta(minutes=80))
    for t in trips:
        for m in range(0, 60, 5):
            ping(db, t, m)
    assert compute_shields(db, trips, AGENCY) == {}


def test_shields_flag_a_mid_window_collapse_for_the_right_agency(db):
    # 40 OpA trips ping every 5 min except minutes 20-39 (two whole buckets
    # where only 4/40 report); 40 OpB trips report throughout. Only OpA is
    # shielded, and the interval covers the collapse.
    a, b = _fleet(40, "R1"), _fleet(40, "R2")
    beat_span(db, T0 - dt.timedelta(minutes=10), T0 + dt.timedelta(minutes=80))
    for i, t in enumerate(a):
        for m in range(0, 60, 5):
            if 20 <= m < 40 and i >= 4:
                continue
            ping(db, t, m)
    for t in b:
        for m in range(0, 60, 5):
            ping(db, t, m)
    shields = compute_shields(db, a + b, AGENCY)
    assert set(shields) == {"OpA"}
    (start, end), = shields["OpA"]
    assert start == T0 + dt.timedelta(minutes=20)
    assert end == T0 + dt.timedelta(minutes=40)


def test_shields_ignore_collapse_during_tracker_downtime(db):
    # Same collapse, but the tracker itself was down minutes 20-39: those
    # buckets are unwatched, so no shield - EXCLUDED owns those trips.
    trips = _fleet(40)
    beat_span(db, T0 - dt.timedelta(minutes=10), T0 + dt.timedelta(minutes=20))
    beat_span(db, T0 + dt.timedelta(minutes=40), T0 + dt.timedelta(minutes=80))
    for t in trips:
        for m in range(0, 60, 5):
            if 20 <= m < 40:
                continue
            ping(db, t, m)
    assert compute_shields(db, trips, AGENCY) == {}


def test_shields_need_minimum_fleet(db):
    # 10 active trips all going silent is noise, not a feed event.
    trips = _fleet(10)
    beat_span(db, T0 - dt.timedelta(minutes=10), T0 + dt.timedelta(minutes=80))
    for t in trips:
        ping(db, t, 0)
    assert compute_shields(db, trips, AGENCY) == {}


def test_route_agency_map_reads_gtfs_and_degrades_to_empty(db):
    assert route_agency_map(db) == {}  # no gtfs tables yet -> no shields ever
    db.executescript(
        "CREATE TABLE gtfs_routes (route_id TEXT PRIMARY KEY, agency_id TEXT,"
        " route_short_name TEXT, route_long_name TEXT);"
        "CREATE TABLE gtfs_agency (agency_id TEXT PRIMARY KEY, agency_name TEXT);")
    db.execute("INSERT INTO gtfs_routes VALUES ('R1','A1',NULL,NULL)")
    db.execute("INSERT INTO gtfs_agency VALUES ('A1','OpA')")
    assert route_agency_map(db) == {"R1": "OpA"}


def test_bucket_index_is_stable_and_aligned():
    assert bucket_index(T0) == bucket_index(T0 + dt.timedelta(seconds=BUCKET_S - 1))
    assert bucket_index(T0 + dt.timedelta(seconds=BUCKET_S)) == bucket_index(T0) + 1
