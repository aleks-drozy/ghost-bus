import datetime as dt
import sqlite3
from pathlib import Path

import pytest
from google.transit import gtfs_realtime_pb2 as rt

from classify.store import init_store
from ingest.poller import parse_feed, poll_once

UTC = dt.timezone.utc


def make_feed(entities):
    feed = rt.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = 1774252800
    for e in entities:
        feed.entity.append(e)
    return feed.SerializeToString()


def trip_update(trip_id, start_date="20260323", cancelled=False, max_seq=None):
    e = rt.FeedEntity()
    e.id = f"tu-{trip_id}"
    e.trip_update.trip.trip_id = trip_id
    e.trip_update.trip.start_date = start_date
    if cancelled:
        e.trip_update.trip.schedule_relationship = rt.TripDescriptor.CANCELED
    if max_seq is not None:
        for seq in (1, max_seq):
            stu = e.trip_update.stop_time_update.add()
            stu.stop_sequence = seq
    return e


def vehicle(trip_id, seq, start_date="20260323", lat=None, lon=None, ts=None):
    e = rt.FeedEntity()
    e.id = f"v-{trip_id}"
    e.vehicle.trip.trip_id = trip_id
    e.vehicle.trip.start_date = start_date
    e.vehicle.current_stop_sequence = seq
    if lat is not None:
        e.vehicle.position.latitude = lat
        e.vehicle.position.longitude = lon
    if ts is not None:
        e.vehicle.timestamp = ts
    return e


def test_parse_kinds():
    raw = make_feed([trip_update("A", max_seq=4), trip_update("B", cancelled=True),
                     vehicle("C", 2)])
    obs = {o["trip_id"]: o for o in parse_feed(raw)}
    assert obs["A"]["kind"] == "update" and obs["A"]["stop_sequence"] == 4
    assert obs["B"]["kind"] == "cancel"
    assert obs["C"]["kind"] == "position" and obs["C"]["stop_sequence"] == 2
    assert obs["A"]["start_date"] == "20260323"


def test_poll_once_records_heartbeat_and_observations(tmp_path):
    db = sqlite3.connect(":memory:")
    init_store(db)
    raw = make_feed([vehicle("C", 2)])
    now = dt.datetime(2026, 3, 23, 7, 0, tzinfo=UTC)
    n = poll_once(db, fetch_fn=lambda: raw, now_fn=lambda: now,
                  route_filter=None, archive_dir=tmp_path)
    assert n == 1
    assert db.execute("SELECT ok FROM heartbeats").fetchone() == (1,)
    (sd,) = db.execute("SELECT service_date FROM observations").fetchone()
    assert sd == "2026-03-23"
    archived = list(Path(tmp_path).rglob("*.pb.zst"))
    assert len(archived) == 1


def test_fetch_failure_records_bad_heartbeat(tmp_path):
    db = sqlite3.connect(":memory:")
    init_store(db)

    def boom():
        raise ConnectionError("api down")

    now = dt.datetime(2026, 3, 23, 7, 0, tzinfo=UTC)
    n = poll_once(db, fetch_fn=boom, now_fn=lambda: now, route_filter=None, archive_dir=None)
    assert n == -1
    assert db.execute("SELECT ok FROM heartbeats").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM observations").fetchone() == (0,)


def test_position_stop_sequence_zero_is_preserved():
    raw = make_feed([vehicle("Z", 0)])
    (o,) = parse_feed(raw)
    assert o["stop_sequence"] == 0  # real zero, not None


def test_update_without_stop_sequences_gives_none():
    raw = make_feed([trip_update("A")])
    (o,) = parse_feed(raw)
    assert o["kind"] == "update" and o["stop_sequence"] is None


def test_empty_trip_id_skipped(tmp_path):
    db = sqlite3.connect(":memory:")
    init_store(db)
    raw = make_feed([vehicle("", 2)])
    now = dt.datetime(2026, 3, 23, 7, 0, tzinfo=UTC)
    n = poll_once(db, fetch_fn=lambda: raw, now_fn=lambda: now,
                  route_filter=None, archive_dir=None)
    assert n == 0
    assert db.execute("SELECT COUNT(*) FROM observations").fetchone() == (0,)


def test_unparseable_feed_is_a_failed_poll(tmp_path):
    db = sqlite3.connect(":memory:")
    init_store(db)
    now = dt.datetime(2026, 3, 23, 7, 0, tzinfo=UTC)
    n = poll_once(db, fetch_fn=lambda: b"<html>gateway error</html>", now_fn=lambda: now,
                  route_filter=None, archive_dir=tmp_path)
    assert n == -1
    assert db.execute("SELECT ok FROM heartbeats").fetchone() == (0,)
    assert list(Path(tmp_path).rglob("*.zst")) == []


def test_unmatchable_start_date_is_skipped():
    db = sqlite3.connect(":memory:")
    init_store(db)
    raw = make_feed([vehicle("Z", 2, start_date="")])
    now = dt.datetime(2026, 3, 23, 7, 0, tzinfo=UTC)
    n = poll_once(db, fetch_fn=lambda: raw, now_fn=lambda: now,
                  route_filter=None, archive_dir=None)
    assert n == 0
    assert db.execute("SELECT COUNT(*) FROM observations").fetchone() == (0,)


def test_parse_vehicle_position_coordinates():
    raw = make_feed([vehicle("C", 2, lat=53.3492, lon=-6.2603)])
    (o,) = parse_feed(raw)
    assert o["lat"] == pytest.approx(53.3492, abs=1e-4)
    assert o["lon"] == pytest.approx(-6.2603, abs=1e-4)


def test_parse_vehicle_without_position_gives_none():
    raw = make_feed([vehicle("C", 2)])
    (o,) = parse_feed(raw)
    assert o["lat"] is None and o["lon"] is None


def test_parse_updates_and_cancels_carry_no_coordinates():
    raw = make_feed([trip_update("A", max_seq=4), trip_update("B", cancelled=True)])
    for o in parse_feed(raw):
        assert o["lat"] is None and o["lon"] is None


def test_parse_vehicle_timestamp_captured():
    vts = dt.datetime(2026, 3, 23, 6, 59, tzinfo=UTC)
    raw = make_feed([vehicle("C", 2, ts=int(vts.timestamp()))])
    (o,) = parse_feed(raw)
    assert o["vehicle_ts"] == "2026-03-23T06:59:00+00:00"


def test_parse_vehicle_without_timestamp_gives_none():
    raw = make_feed([vehicle("C", 2)])  # unset uint64 reads back as 0
    (o,) = parse_feed(raw)
    assert o["vehicle_ts"] is None


def test_parse_vehicle_explicit_zero_timestamp_gives_none():
    raw = make_feed([vehicle("C", 2, ts=0)])  # 1970 epoch is never a real report
    (o,) = parse_feed(raw)
    assert o["vehicle_ts"] is None


def test_parse_updates_and_cancels_carry_no_vehicle_ts():
    raw = make_feed([trip_update("A", max_seq=4), trip_update("B", cancelled=True)])
    for o in parse_feed(raw):
        assert o["vehicle_ts"] is None


def test_poll_once_stores_coordinates(tmp_path):
    db = sqlite3.connect(":memory:")
    init_store(db)
    raw = make_feed([vehicle("C", 2, lat=53.3492, lon=-6.2603)])
    now = dt.datetime(2026, 3, 23, 7, 0, tzinfo=UTC)
    poll_once(db, fetch_fn=lambda: raw, now_fn=lambda: now,
              route_filter=None, archive_dir=None)
    (lat, lon) = db.execute("SELECT lat, lon FROM observations").fetchone()
    assert lat == pytest.approx(53.3492, abs=1e-4)
    assert lon == pytest.approx(-6.2603, abs=1e-4)


def test_poll_once_stores_vehicle_ts():
    db = sqlite3.connect(":memory:")
    init_store(db)
    vts = dt.datetime(2026, 3, 23, 6, 59, tzinfo=UTC)
    raw = make_feed([vehicle("C", 2, lat=53.3492, lon=-6.2603,
                             ts=int(vts.timestamp()))])
    now = dt.datetime(2026, 3, 23, 7, 0, tzinfo=UTC)
    poll_once(db, fetch_fn=lambda: raw, now_fn=lambda: now,
              route_filter=None, archive_dir=None)
    (vehicle_ts, ts_utc) = db.execute(
        "SELECT vehicle_ts, ts_utc FROM observations").fetchone()
    assert vehicle_ts == "2026-03-23T06:59:00+00:00"  # vehicle's report, 60 s before poll
    assert ts_utc == "2026-03-23T07:00:00+00:00"
