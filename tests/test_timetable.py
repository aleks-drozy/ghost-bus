import datetime as dt
import sqlite3

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
    # EU spring-forward: 2026-03-29 01:00Z (01:00 GMT -> 02:00 IST).
    # "24:30:00" on service day 2026-03-28 = 24.5 elapsed hours after
    # 2026-03-28 00:00Z -> 2026-03-29 00:30Z, BEFORE the jump -> 00:30 GMT.
    local = local_from_service(dt.date(2026, 3, 28), gtfs_seconds("24:30:00"), "Europe/Dublin")
    assert local.utcoffset() == dt.timedelta(0)
    assert local.astimezone(UTC) == dt.datetime(2026, 3, 29, 0, 30, tzinfo=UTC)


def test_noon_rule_crosses_spring_forward_gap():
    # "25:30:00" = 25.5 elapsed hours -> 2026-03-29 01:30Z, AFTER the jump -> 02:30 IST.
    local = local_from_service(dt.date(2026, 3, 28), gtfs_seconds("25:30:00"), "Europe/Dublin")
    assert local.utcoffset() == dt.timedelta(hours=1)
    assert local.hour == 2 and local.minute == 30
    assert local.astimezone(UTC) == dt.datetime(2026, 3, 29, 1, 30, tzinfo=UTC)


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
    assert t0.max_stop_seq == 5


def test_scheduled_trips_saturday(db):
    trips = scheduled_trips(db, dt.date(2026, 3, 28))
    assert {t.trip_id for t in trips} == {"R2_sat_00"}


def test_out_of_range_date_is_empty(db):
    assert scheduled_trips(db, dt.date(2026, 5, 1)) == []


def test_calendar_dates_exceptions(db):
    # 2026-04-01 is a Wednesday: WK removed (bank holiday), SAT added.
    trips = scheduled_trips(db, dt.date(2026, 4, 1))
    assert {t.trip_id for t in trips} == {"R2_sat_00"}
