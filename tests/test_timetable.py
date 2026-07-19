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
    assert n_trips == 19  # 11 R1 + 5 R2 wk + 1 R2 sat + 2 R3 wk


def test_load_stores_agency_and_routes(db):
    agencies = dict(db.execute("SELECT agency_id, agency_name FROM gtfs_agency"))
    assert agencies == {"FVB": "Fixtureville Bus", "GAI": "Go-Ahead Fixtureville"}
    routes = dict(db.execute("SELECT route_id, agency_id FROM gtfs_routes"))
    assert routes == {"R1": "FVB", "R2": "FVB", "R3": "GAI"}


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
    assert "R3_wk_00" in ids and "R3_wk_01" in ids
    assert len(trips) == 18  # 11 R1 + 5 R2 wk + 2 R3 wk
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


def test_scheduled_trips_agency_filter_excludes_other_agency(db):
    trips = scheduled_trips(db, dt.date(2026, 3, 23), agency_names={"Fixtureville Bus"})
    ids = {t.trip_id for t in trips}
    assert "R3_wk_00" not in ids and "R3_wk_01" not in ids
    assert len(trips) == 16  # 11 R1 + 5 R2 wk, R3 (GAI) excluded


def test_scheduled_trips_agency_filter_none_returns_all(db):
    trips = scheduled_trips(db, dt.date(2026, 3, 23), agency_names=None)
    assert len(trips) == 18


def test_scheduled_trips_agency_filter_unknown_name_returns_empty(db):
    assert scheduled_trips(db, dt.date(2026, 3, 23), agency_names={"Nonexistent Co"}) == []


def test_load_stores_stop_coordinates_and_skips_uncodable(db):
    stops = {sid: (lat, lon) for sid, lat, lon
             in db.execute("SELECT stop_id, lat, lon FROM gtfs_stops")}
    assert stops["S1"] == (pytest.approx(53.3000), pytest.approx(-6.2000))
    assert stops["S3"] == (pytest.approx(53.3072), pytest.approx(-6.2000))
    assert "SBAD" not in stops  # blank coordinates -> skipped, never stored
    assert len(stops) == 7


def test_load_stores_stop_id_in_stop_times(db):
    rows = db.execute(
        "SELECT stop_sequence, stop_id FROM gtfs_stop_times "
        "WHERE trip_id='R1_wk_00' ORDER BY stop_sequence").fetchall()
    assert rows == [(1, "S1"), (2, "S2"), (3, "S3"), (4, "S4"), (5, "S5")]


def test_load_stores_route_names(db):
    rows = dict((rid, (s, l)) for rid, s, l in db.execute(
        "SELECT route_id, route_short_name, route_long_name FROM gtfs_routes"))
    assert rows["R1"] == ("1", "Fixtureville Main")
    assert rows["R3"] == ("3", "Fixtureville Crosstown")


def test_load_migrates_legacy_schema(tmp_path):
    # A DB created by the pre-G1 loader: no stop_id column, no name columns,
    # no gtfs_stops table. load_gtfs must migrate it in place.
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE gtfs_stop_times (trip_id TEXT, stop_sequence INTEGER, dep_seconds INTEGER);"
        "CREATE TABLE gtfs_routes (route_id TEXT PRIMARY KEY, agency_id TEXT);")
    zip_path = tmp_path / "f.zip"
    build_gtfs_zip(zip_path)
    load_gtfs(zip_path, conn)
    (sid,) = conn.execute("SELECT stop_id FROM gtfs_stop_times "
                          "WHERE trip_id='R1_wk_00' AND stop_sequence=1").fetchone()
    assert sid == "S1"
    (short,) = conn.execute(
        "SELECT route_short_name FROM gtfs_routes WHERE route_id='R1'").fetchone()
    assert short == "1"
    (n,) = conn.execute("SELECT COUNT(*) FROM gtfs_stops").fetchone()
    assert n == 7
