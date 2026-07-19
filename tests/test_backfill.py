"""Offline tests for the archive GPS backfill tool (no network, tmp dirs only)."""
import datetime as dt
import sqlite3
from pathlib import Path

import pytest
import zstandard
from google.transit import gtfs_realtime_pb2 as rt

from classify.store import init_store, record_observation
from ingest.backfill import (SchemaTooOld, backfill_archive, backfill_file, main,
                             ts_prefix_from_path)

# The archive stores whole poll snapshots; a ts_utc keeps the microseconds and
# offset the poller wrote, of which the file path encodes the first 19 chars.
TS_PREFIX = "2026-07-18T21:51:41"
TS_UTC = "2026-07-18T21:51:41.123456+00:00"


def make_feed(entities):
    feed = rt.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    for e in entities:
        feed.entity.append(e)
    return feed.SerializeToString()


def vehicle(trip_id, lat=53.3492, lon=-6.2603, start_date="20260718", with_position=True):
    e = rt.FeedEntity()
    e.id = f"v-{trip_id}"
    e.vehicle.trip.trip_id = trip_id
    e.vehicle.trip.start_date = start_date
    if with_position:
        e.vehicle.position.latitude = lat
        e.vehicle.position.longitude = lon
    return e


def trip_update(trip_id, start_date="20260718"):
    e = rt.FeedEntity()
    e.id = f"tu-{trip_id}"
    e.trip_update.trip.trip_id = trip_id
    e.trip_update.trip.start_date = start_date
    return e


def fresh_db():
    db = sqlite3.connect(":memory:")
    init_store(db)
    return db


def coords(db, trip_id="A"):
    return db.execute("SELECT lat, lon FROM observations WHERE trip_id=?",
                      (trip_id,)).fetchone()


def test_ts_prefix_from_valid_path(tmp_path):
    p = tmp_path / "archive" / "20260718" / "215141.pb.zst"
    assert ts_prefix_from_path(p) == "2026-07-18T21:51:41"


def test_ts_prefix_malformed_returns_none(tmp_path):
    a = tmp_path / "archive"
    for day, name in [("20260718", "9999.pb.zst"),       # wrong length
                      ("20260718", "abcdef.pb.zst"),     # non-digit time
                      ("2026071x", "215141.pb.zst"),     # non-digit day
                      ("20260718", "256161.pb.zst"),     # invalid hh/mm/ss
                      ("20261318", "215141.pb.zst"),      # invalid month
                      ("20260718", "2151.pb.zst"),        # strptime would backtrack
                      ("20260718", "٢١٥١٤١.pb.zst")]:     # non-ascii digits
        assert ts_prefix_from_path(a / day / name) is None


def test_apply_fills_null_coordinates():
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 3)
    res = backfill_file(db, make_feed([vehicle("A")]), TS_PREFIX, apply=True)
    lat, lon = coords(db)
    assert lat == pytest.approx(53.3492, abs=1e-4)
    assert lon == pytest.approx(-6.2603, abs=1e-4)
    assert (res.pings, res.filled, res.already_filled, res.no_row) == (1, 1, 0, 0)


def test_dry_run_reports_but_writes_nothing():
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 3)
    res = backfill_file(db, make_feed([vehicle("A")]), TS_PREFIX, apply=False)
    assert coords(db) == (None, None)
    assert (res.pings, res.filled) == (1, 1)  # "filled" = would fill


def test_never_overwrites_existing_coordinates():
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 3, lat=1.0, lon=2.0)
    res = backfill_file(db, make_feed([vehicle("A")]), TS_PREFIX, apply=True)
    assert coords(db) == (1.0, 2.0)
    assert (res.filled, res.already_filled) == (0, 1)


def test_second_apply_run_is_a_no_op():
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 3)
    raw = make_feed([vehicle("A")])
    backfill_file(db, raw, TS_PREFIX, apply=True)
    res = backfill_file(db, raw, TS_PREFIX, apply=True)
    assert (res.filled, res.already_filled) == (0, 1)


def test_ping_with_no_stored_observation_is_counted_not_invented():
    db = fresh_db()
    res = backfill_file(db, make_feed([vehicle("A")]), TS_PREFIX, apply=True)
    assert db.execute("SELECT COUNT(*) FROM observations").fetchone() == (0,)
    assert (res.pings, res.filled, res.no_row) == (1, 0, 1)


def test_other_polls_of_the_same_trip_are_untouched():
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 3)
    record_observation(db, "A", "2026-07-18", "2026-07-18T21:52:41.9+00:00", "position", 4)
    backfill_file(db, make_feed([vehicle("A")]), TS_PREFIX, apply=True)
    rows = db.execute("SELECT stop_sequence, lat FROM observations ORDER BY ts_utc").fetchall()
    assert rows[0][1] is not None and rows[1][1] is None


def test_update_and_cancel_rows_are_never_given_coordinates():
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "update", 3)
    res = backfill_file(db, make_feed([vehicle("A")]), TS_PREFIX, apply=True)
    assert coords(db) == (None, None)
    assert (res.filled, res.no_row) == (0, 1)


def test_vehicle_without_position_contributes_nothing():
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 3)
    res = backfill_file(db, make_feed([vehicle("A", with_position=False)]),
                        TS_PREFIX, apply=True)
    assert coords(db) == (None, None)
    assert (res.pings, res.filled) == (0, 0)


def test_trip_update_entities_are_ignored():
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "update", 3)
    res = backfill_file(db, make_feed([trip_update("A")]), TS_PREFIX, apply=True)
    assert res.pings == 0


def test_unusable_keys_are_skipped_exactly_as_the_poller_skips_them():
    db = fresh_db()
    raw = make_feed([vehicle(""), vehicle("B", start_date=""),
                     vehicle("C", start_date="2026071")])
    res = backfill_file(db, raw, TS_PREFIX, apply=True)
    assert res.pings == 0  # no trip_id / unmatchable service date


# --- archive walk -----------------------------------------------------------

def write_snapshot(archive, day, hhmmss, entities):
    d = archive / day
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{hhmmss}.pb.zst"
    p.write_bytes(zstandard.ZstdCompressor().compress(make_feed(entities)))
    return p


def test_walks_every_day_directory(tmp_path):
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 1)
    record_observation(db, "B", "2026-07-19", "2026-07-19T06:00:00.5+00:00", "position", 1)
    archive = tmp_path / "archive"
    write_snapshot(archive, "20260718", "215141", [vehicle("A")])
    write_snapshot(archive, "20260719", "060000", [vehicle("B", start_date="20260719")])
    res = backfill_archive(db, archive, apply=True)
    assert (res.files, res.filled) == (2, 2)
    assert coords(db, "A")[0] is not None and coords(db, "B")[0] is not None


def test_corrupt_snapshot_does_not_abort_the_run(tmp_path):
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 1)
    archive = tmp_path / "archive"
    (archive / "20260718").mkdir(parents=True)
    (archive / "20260718" / "215140.pb.zst").write_bytes(b"this is not zstd")
    write_snapshot(archive, "20260718", "215141", [vehicle("A")])
    res = backfill_archive(db, archive, apply=True)
    assert (res.unreadable, res.filled) == (1, 1)


def test_valid_zstd_that_is_not_a_feed_counts_as_unreadable(tmp_path):
    db = fresh_db()
    archive = tmp_path / "archive"
    (archive / "20260718").mkdir(parents=True)
    (archive / "20260718" / "215141.pb.zst").write_bytes(
        zstandard.ZstdCompressor().compress(b"<html>gateway error</html>"))
    res = backfill_archive(db, archive, apply=True)
    assert (res.unreadable, res.files) == (1, 0)


def test_stray_file_with_unparseable_name_is_skipped(tmp_path):
    db = fresh_db()
    archive = tmp_path / "archive"
    (archive / "20260718").mkdir(parents=True)
    (archive / "20260718" / "notes.txt").write_text("scratch")
    res = backfill_archive(db, archive, apply=True)
    assert (res.files, res.unreadable) == (0, 0)


def test_snapshot_with_undecodable_name_is_counted_not_guessed(tmp_path):
    db = fresh_db()
    archive = tmp_path / "archive"
    (archive / "20260718").mkdir(parents=True)
    (archive / "20260718" / "2151.pb.zst").write_bytes(
        zstandard.ZstdCompressor().compress(make_feed([vehicle("A")])))
    res = backfill_archive(db, archive, apply=True)
    assert (res.unreadable, res.files, res.filled) == (1, 0, 0)


def test_files_are_visited_in_chronological_order(tmp_path):
    db = fresh_db()
    archive = tmp_path / "archive"
    for day, hhmmss in [("20260719", "060000"), ("20260718", "215141"),
                        ("20260718", "090000")]:
        write_snapshot(archive, day, hhmmss, [vehicle("A")])
    seen = []
    backfill_archive(db, archive, apply=False, progress_fn=lambda p, c: seen.append(p.parent.name + p.name))
    assert seen == ["20260718090000.pb.zst", "20260718215141.pb.zst",
                    "20260719060000.pb.zst"]


def test_days_filter_restricts_the_walk(tmp_path):
    db = fresh_db()
    archive = tmp_path / "archive"
    write_snapshot(archive, "20260718", "215141", [vehicle("A")])
    write_snapshot(archive, "20260719", "060000", [vehicle("B", start_date="20260719")])
    res = backfill_archive(db, archive, apply=False, days={"20260719"})
    assert res.files == 1


def test_missing_archive_directory_is_reported_not_crashed(tmp_path):
    db = fresh_db()
    res = backfill_archive(db, tmp_path / "nope", apply=False)
    assert (res.files, res.pings) == (0, 0)


def test_round_trip_against_the_real_poller(tmp_path):
    """The strongest guarantee available offline: let the actual poller write
    both the archive file and the observation, blank the coordinates to
    reproduce a pre-G1 row, and prove the backfill puts them back. If the key
    derivation ever drifts from the writer's, this test fails and the narrower
    unit tests would not."""
    from ingest.poller import poll_once

    db = fresh_db()
    archive = tmp_path / "archive"
    now = dt.datetime(2026, 7, 18, 21, 51, 41, 123456, tzinfo=dt.timezone.utc)
    raw = make_feed([vehicle("A", lat=53.3, lon=-6.25),
                     vehicle("B", lat=53.4, lon=-6.30)])
    poll_once(db, fetch_fn=lambda: raw, now_fn=lambda: now,
              route_filter=None, archive_dir=archive)
    db.execute("UPDATE observations SET lat=NULL, lon=NULL")  # pre-G1 poller
    db.commit()

    res = backfill_archive(db, archive, apply=True)
    assert (res.files, res.pings, res.filled, res.no_row) == (1, 2, 2, 0)
    assert coords(db, "A")[0] == pytest.approx(53.3, abs=1e-4)
    assert coords(db, "B")[1] == pytest.approx(-6.30, abs=1e-4)


# --- pre-G1 schema ----------------------------------------------------------

PRE_G1_SCHEMA = """
CREATE TABLE observations (
  trip_id TEXT, service_date TEXT, ts_utc TEXT, kind TEXT, stop_sequence INTEGER);
CREATE TABLE heartbeats (ts_utc TEXT PRIMARY KEY, ok INTEGER);
"""


def pre_g1_db(path=":memory:"):
    """The schema actually on the live VM today - G1 is not deployed there."""
    db = sqlite3.connect(path)
    db.executescript(PRE_G1_SCHEMA)
    db.execute("INSERT INTO observations VALUES ('A','2026-07-18',?,'position',1)",
               (TS_UTC,))
    db.commit()
    return db


def test_database_without_coordinate_columns_fails_fast(tmp_path):
    db = pre_g1_db()
    archive = tmp_path / "archive"
    write_snapshot(archive, "20260718", "215141", [vehicle("A")])
    with pytest.raises(SchemaTooOld) as exc:
        backfill_archive(db, archive, apply=True)
    assert "lat" in str(exc.value)


def test_cli_on_pre_g1_database_explains_the_missing_migration(tmp_path, capsys):
    db_path = tmp_path / "ghostbus.db"
    pre_g1_db(db_path).close()
    archive = tmp_path / "archive"
    write_snapshot(archive, "20260718", "215141", [vehicle("A")])
    rc = main(["--db", str(db_path), "--archive", str(archive), "--apply"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "lat" in err and "poller" in err


# --- CLI --------------------------------------------------------------------

def seed_cli_case(tmp_path):
    db_path = tmp_path / "ghostbus.db"
    db = sqlite3.connect(db_path)
    init_store(db)
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 1)
    db.close()
    archive = tmp_path / "archive"
    write_snapshot(archive, "20260718", "215141", [vehicle("A")])
    return db_path, archive


def stored_coords(db_path):
    db = sqlite3.connect(db_path)
    try:
        return db.execute("SELECT lat, lon FROM observations").fetchone()
    finally:
        db.close()


def test_cli_defaults_to_dry_run(tmp_path, capsys):
    db_path, archive = seed_cli_case(tmp_path)
    rc = main(["--db", str(db_path), "--archive", str(archive)])
    assert rc == 0
    assert stored_coords(db_path) == (None, None)
    assert "DRY RUN" in capsys.readouterr().out


def test_cli_apply_writes_coordinates(tmp_path, capsys):
    db_path, archive = seed_cli_case(tmp_path)
    rc = main(["--db", str(db_path), "--archive", str(archive), "--apply"])
    assert rc == 0
    assert stored_coords(db_path)[0] == pytest.approx(53.3492, abs=1e-4)
