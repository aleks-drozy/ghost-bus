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


def vehicle(trip_id, lat=53.3492, lon=-6.2603, start_date="20260718", with_position=True,
           timestamp=0):
    e = rt.FeedEntity()
    e.id = f"v-{trip_id}"
    e.vehicle.trip.trip_id = trip_id
    e.vehicle.trip.start_date = start_date
    if with_position:
        e.vehicle.position.latitude = lat
        e.vehicle.position.longitude = lon
    if timestamp:
        e.vehicle.timestamp = timestamp
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
                      ("20260718", "٢١٥١٤١.pb.zst"),      # non-ascii digits
                      ("20260718", "215141.bak.pb.zst"),  # Finding F1(b): stale-copy
                                                           # suffix before .pb.zst - must
                                                           # NOT resolve to the same
                                                           # prefix as the real file
                      ("20260718", "215141.1.pb.zst"),    # numbered-copy suffix, same reason
                      ("20260718", "215141.tmp.pb.zst")]:  # in-progress-write suffix, same reason
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


# --- ambiguous matches (Finding 1) -------------------------------------------

def test_two_entities_sharing_a_trip_id_in_one_snapshot_write_neither_row(capsys):
    """Two distinct vehicles reported the same trip_id in the same poll (the
    poller stored both as separate NULL rows). The archive replay can't tell
    which entity belongs to which row, so it must refuse to guess: neither
    row is written, and the ambiguity is counted, not silently dropped.

    The in-file path used to increment the counter with no stderr at all,
    leaving an operator no way to find which trip caused a nonzero
    `ambiguous` short of hand-writing a GROUP BY query (RUNBOOK 7.1). It must
    now name the trip on stderr and identify this as the shared-key
    condition - distinct wording from the duplicate-stored-rows condition,
    since the two mean different things to an operator."""
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 1)
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 2)
    res = backfill_file(db, make_feed([vehicle("A", lat=53.1, lon=-6.1),
                                       vehicle("A", lat=53.9, lon=-6.9)]),
                        TS_PREFIX, apply=True)
    rows = db.execute("SELECT lat, lon FROM observations WHERE trip_id='A'").fetchall()
    assert rows == [(None, None), (None, None)]
    assert res.ambiguous == 2
    assert (res.filled, res.already_filled) == (0, 0)
    err = capsys.readouterr().err
    assert "A" in err and TS_PREFIX in err
    assert "2 pings" in err and "share" in err
    assert "stored row" not in err  # must not read like the other condition


def test_ordinary_single_match_still_writes_correctly_alongside_ambiguity_guard():
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 1)
    res = backfill_file(db, make_feed([vehicle("A", lat=53.1, lon=-6.1)]),
                        TS_PREFIX, apply=True)
    assert coords(db) == (pytest.approx(53.1, abs=1e-4), pytest.approx(-6.1, abs=1e-4))
    assert (res.filled, res.ambiguous) == (1, 0)


def test_one_stored_row_two_entities_sharing_key_is_ambiguous_not_guessed():
    """The ambiguity guard must key off the SNAPSHOT, not stored-row count.
    Only one stored row exists for trip "A", but the snapshot itself carries
    two distinct pings for that same join key - a crash/OOM-kill mid-poll
    (RUNBOOK 7.1's "no stored observation" case, mirrored here) can leave
    the archive ahead of the DB, so rows < entities is a real state. The old
    row-count-only guard let entity 1 write, after which entity 2's probe
    saw entity 1's own UPDATE and got folded into "already_filled" - a
    genuinely distinct GPS reading silently discarded with no ambiguous
    counter and no stderr. Neither entity may be written; both must count
    as ambiguous."""
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 1)
    res = backfill_file(db, make_feed([vehicle("A", lat=53.1, lon=-6.1),
                                       vehicle("A", lat=53.9, lon=-6.9)]),
                        TS_PREFIX, apply=True)
    assert coords(db) == (None, None)
    assert res.ambiguous == 2
    assert (res.filled, res.already_filled) == (0, 0)


def test_zero_stored_rows_two_entities_sharing_key_is_ambiguous_not_a_crash():
    """No stored row at all for the key, but the snapshot still carries two
    pings that collide on it. Must not crash, and must still be counted as
    ambiguous rather than no_row - there is genuinely no way to tell which
    entity a future row would belong to."""
    db = fresh_db()
    res = backfill_file(db, make_feed([vehicle("A", lat=53.1, lon=-6.1),
                                       vehicle("A", lat=53.9, lon=-6.9)]),
                        TS_PREFIX, apply=True)
    assert res.ambiguous == 2
    assert (res.filled, res.already_filled, res.no_row) == (0, 0, 0)


def test_one_ping_matching_multiple_stored_rows_is_ambiguous_and_reported(capsys):
    """The snapshot carries exactly one ping for this key, but more than one
    stored row already matches it (e.g. two rows landed with the same
    trip_id/service_date/second-resolution ts_utc for reasons upstream of
    this tool). Writing would risk pinning this ping's real coordinates onto
    the wrong physical row, so it must be refused like the shared-key case -
    but the operator needs different wording: this is duplicate stored rows,
    not two vehicles colliding in one poll, and those point at different
    root causes."""
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 1)
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 2)
    res = backfill_file(db, make_feed([vehicle("A", lat=53.1, lon=-6.1)]),
                        TS_PREFIX, apply=True)
    rows = db.execute("SELECT lat, lon FROM observations WHERE trip_id='A'").fetchall()
    assert rows == [(None, None), (None, None)]
    assert res.ambiguous == 1
    assert (res.filled, res.already_filled) == (0, 0)
    err = capsys.readouterr().err
    assert "A" in err and TS_PREFIX in err
    assert "2 stored rows" in err
    assert "share this join key" not in err  # must not read like the other condition


def test_ambiguous_stderr_is_one_line_per_key_not_per_ping(capsys):
    """Proportionality: a pathological snapshot where N pings all share one
    key must not flood stderr with N lines - one line per distinct
    ambiguous key, however many pings share it, so a bad archive can't
    drown the operator in duplicate output."""
    db = fresh_db()
    pings = [vehicle("A", lat=53.1 + i * 0.01, lon=-6.1) for i in range(5)]
    res = backfill_file(db, make_feed(pings), TS_PREFIX, apply=True)
    assert res.ambiguous == 5
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "5 pings" in lines[0]


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


def test_unreadable_snapshot_surfaces_path_and_exception(tmp_path, capsys):
    """Finding 3: a caught _UNREADABLE exception must not vanish. A future
    logic bug hiding behind the same broad catch tuple has to be
    diagnosable, not indistinguishable from ordinary archive corruption."""
    db = fresh_db()
    archive = tmp_path / "archive"
    (archive / "20260718").mkdir(parents=True)
    bad = archive / "20260718" / "215140.pb.zst"
    bad.write_bytes(b"this is not zstd")
    backfill_archive(db, archive, apply=True)
    err = capsys.readouterr().err
    assert bad.name in err
    assert "ZstdError" in err


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


def test_snapshot_with_undecodable_name_is_counted_not_guessed(tmp_path, capsys):
    """RUNBOOK 7.1 promises every unreadable snapshot - including one with
    an unrecognisable filename - is printed to stderr with its path. Assert
    on the actual captured stderr, not just the counter, so the docs and
    the code can't silently drift apart again."""
    db = fresh_db()
    archive = tmp_path / "archive"
    (archive / "20260718").mkdir(parents=True)
    bad = archive / "20260718" / "2151.pb.zst"
    bad.write_bytes(zstandard.ZstdCompressor().compress(make_feed([vehicle("A")])))
    res = backfill_archive(db, archive, apply=True)
    assert (res.unreadable, res.files, res.filled) == (1, 0, 0)
    err = capsys.readouterr().err
    assert bad.name in err
    assert "filename" in err


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


# --- cross-file key collisions (Finding F1) ----------------------------------
#
# ts_prefix is constant per file, so two files can only share a join key if
# they share a ts_prefix. v3 rebuilt its ping-grouping dict inside
# backfill_file, called once per file, so it only ever saw collisions WITHIN
# one snapshot. Two files that key to the same second - via a nested subtree
# (F1a) or a stray suffix ts_prefix_from_path used to swallow (F1b) - sailed
# straight past every guard and reproduced the original corruption.

def test_cross_file_collision_via_nested_directories_refuses_to_write(tmp_path):
    """F1(a): rglob is recursive but ts_prefix_from_path only reads
    path.parent.name, so archive/a/20260718/215141.pb.zst and
    archive/b/20260718/215141.pb.zst both key to the same (trip_id,
    service_date, ts_prefix). Neither vehicle's ping may be written."""
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 1)
    archive = tmp_path / "archive"
    write_snapshot(archive / "a", "20260718", "215141", [vehicle("A", lat=53.1, lon=-6.1)])
    write_snapshot(archive / "b", "20260718", "215141", [vehicle("A", lat=53.9, lon=-6.9)])
    res = backfill_archive(db, archive, apply=True)
    assert coords(db) == (None, None)
    assert res.ambiguous == 2
    assert (res.filled, res.already_filled) == (0, 0)
    # Refused files were still opened and parsed, so they must still be counted:
    # "snapshots read 0; coordinate pings 2" reads as an empty archive, which is
    # the opposite of the truth in the one case the operator most needs to read.
    assert res.files == 2


def test_refused_collisions_stay_visible_in_the_file_accounting(tmp_path):
    """files + unreadable must account for every *.pb.zst the walk opened,
    mixing refused collisions with ordinary snapshots - the invariant an
    operator triaging against RUNBOOK 7.1 relies on."""
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 1)
    archive = tmp_path / "archive"
    write_snapshot(archive / "x", "20260718", "215141", [vehicle("A", lat=53.1, lon=-6.1)])
    write_snapshot(archive / "y", "20260718", "215141", [vehicle("A", lat=53.9, lon=-6.9)])
    for i, sec in enumerate(("215142", "215143", "215144")):
        write_snapshot(archive, "20260718", sec, [vehicle(f"C{i}", lat=53.5, lon=-6.2)])
    res = backfill_archive(db, archive, apply=True)
    on_disk = len(list(archive.rglob("*.pb.zst")))
    assert res.files + res.unreadable == on_disk == 5
    assert res.filled + res.already_filled + res.no_row + res.ambiguous == res.pings
    assert res.ambiguous == 2 and coords(db) == (None, None)


def test_cross_file_collision_names_both_paths_on_stderr(tmp_path, capsys):
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 1)
    archive = tmp_path / "archive"
    p1 = write_snapshot(archive / "a", "20260718", "215141", [vehicle("A", lat=53.1, lon=-6.1)])
    p2 = write_snapshot(archive / "b", "20260718", "215141", [vehicle("A", lat=53.9, lon=-6.9)])
    backfill_archive(db, archive, apply=True)
    err = capsys.readouterr().err
    assert str(p1) in err
    assert str(p2) in err


def test_stale_backup_suffix_no_longer_poisons_the_authoritative_snapshot(tmp_path):
    """F1(b) end-to-end, the exact repro from the finding: a stale .bak copy
    used to sort before the real file (sorted() places ".bak" before ".pb")
    and silently overwrite it with the wrong coordinates, while both the
    summary and a second --apply looked exactly like a clean run. With the
    filename validated in full, the .bak file is now rejected up front as an
    unreadable/unparseable filename rather than being decoded and treated as
    a same-key duplicate - so the authoritative file's ping is the only one
    ever considered, and its real coordinates win."""
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 1)
    archive = tmp_path / "archive"
    day = archive / "20260718"
    day.mkdir(parents=True)
    (day / "215141.pb.zst").write_bytes(zstandard.ZstdCompressor().compress(
        make_feed([vehicle("A", lat=53.1, lon=-6.1)])))       # authoritative
    (day / "215141.bak.pb.zst").write_bytes(zstandard.ZstdCompressor().compress(
        make_feed([vehicle("A", lat=53.9, lon=-6.9)])))       # stale copy
    res = backfill_archive(db, archive, apply=True)
    assert coords(db) == (pytest.approx(53.1, abs=1e-4), pytest.approx(-6.1, abs=1e-4))
    assert res.unreadable == 1
    assert res.ambiguous == 0


def test_two_different_prefixes_in_the_same_day_dir_still_replay_normally(tmp_path):
    """Regression guard: the collision guard must not become a blanket
    refusal for an entire day directory - only files that truly share a
    ts_prefix are held back."""
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 1)
    record_observation(db, "B", "2026-07-18", "2026-07-18T22:00:00+00:00", "position", 1)
    archive = tmp_path / "archive"
    write_snapshot(archive, "20260718", "215141", [vehicle("A")])
    write_snapshot(archive, "20260718", "220000", [vehicle("B")])
    res = backfill_archive(db, archive, apply=True)
    assert (res.files, res.filled, res.ambiguous) == (2, 2, 0)
    assert coords(db, "A")[0] is not None and coords(db, "B")[0] is not None


def test_dry_run_and_apply_agree_on_cross_file_collision_counters(tmp_path):
    """Previously divergent: dry-run reported 'would fill 2, ambiguous 0'
    while --apply reported 'filled 1, already 1', because each colliding
    file was probed and written independently within a single run - the
    second file's probe saw the first file's own UPDATE and looked like
    "already filled". Refusing to replay any colliding file at all makes
    both modes agree exactly, on every counter."""
    db_dry = fresh_db()
    record_observation(db_dry, "A", "2026-07-18", TS_UTC, "position", 1)
    db_apply = fresh_db()
    record_observation(db_apply, "A", "2026-07-18", TS_UTC, "position", 1)
    archive = tmp_path / "archive"
    write_snapshot(archive / "a", "20260718", "215141", [vehicle("A", lat=53.1, lon=-6.1)])
    write_snapshot(archive / "b", "20260718", "215141", [vehicle("A", lat=53.9, lon=-6.9)])

    dry = backfill_archive(db_dry, archive, apply=False)
    applied = backfill_archive(db_apply, archive, apply=True)

    assert (dry.filled, dry.already_filled, dry.ambiguous, dry.pings) == \
           (applied.filled, applied.already_filled, applied.ambiguous, applied.pings)
    assert dry.ambiguous == 2
    assert coords(db_apply) == (None, None)


# --- whole-archive idempotency (Finding 4) -----------------------------------

def test_archive_level_second_apply_run_changes_nothing(tmp_path):
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 1)
    record_observation(db, "B", "2026-07-19", "2026-07-19T06:00:00.5+00:00", "position", 1)
    archive = tmp_path / "archive"
    write_snapshot(archive, "20260718", "215141", [vehicle("A")])
    write_snapshot(archive, "20260719", "060000", [vehicle("B", start_date="20260719")])

    first = backfill_archive(db, archive, apply=True)
    after_first = db.execute(
        "SELECT trip_id, lat, lon FROM observations ORDER BY trip_id").fetchall()

    second = backfill_archive(db, archive, apply=True)
    after_second = db.execute(
        "SELECT trip_id, lat, lon FROM observations ORDER BY trip_id").fetchall()

    assert first.filled == 2
    assert after_first == after_second
    assert (second.filled, second.already_filled) == (0, 2)


# --- vehicle_ts backfill (Finding 2) -----------------------------------------

G1_ONLY_SCHEMA = """
CREATE TABLE observations (
  trip_id TEXT, service_date TEXT, ts_utc TEXT, kind TEXT, stop_sequence INTEGER,
  lat REAL, lon REAL);
CREATE TABLE heartbeats (ts_utc TEXT PRIMARY KEY, ok INTEGER);
"""


def g1_only_db(path=":memory:"):
    """G1 is deployed (lat/lon exist) but the later vehicle_ts migration has
    not landed - a real intermediate state a live VM could be caught in."""
    db = sqlite3.connect(path)
    db.executescript(G1_ONLY_SCHEMA)
    db.commit()
    return db


def insert_g1_row(db, trip_id, service_date, ts_utc, lat=None, lon=None):
    db.execute("INSERT INTO observations "
               "(trip_id, service_date, ts_utc, kind, stop_sequence, lat, lon) "
               "VALUES (?,?,?,'position',1,?,?)", (trip_id, service_date, ts_utc, lat, lon))
    db.commit()


def test_vehicle_ts_column_absent_degrades_cleanly(tmp_path):
    """A database that predates the vehicle_ts migration must still backfill
    lat/lon rather than crash on "no such column: vehicle_ts"."""
    db = g1_only_db()
    insert_g1_row(db, "A", "2026-07-18", TS_UTC)
    archive = tmp_path / "archive"
    write_snapshot(archive, "20260718", "215141", [vehicle("A")])
    res = backfill_archive(db, archive, apply=True)
    assert (res.files, res.filled) == (1, 1)
    lat, lon = db.execute("SELECT lat, lon FROM observations WHERE trip_id='A'").fetchone()
    assert lat == pytest.approx(53.3492, abs=1e-4)
    assert lon == pytest.approx(-6.2603, abs=1e-4)


def test_vehicle_ts_column_present_is_auto_detected_and_backfilled(tmp_path):
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 1)
    ts = int(dt.datetime(2026, 7, 18, 21, 51, 30, tzinfo=dt.timezone.utc).timestamp())
    archive = tmp_path / "archive"
    write_snapshot(archive, "20260718", "215141", [vehicle("A", timestamp=ts)])
    backfill_archive(db, archive, apply=True)
    vehicle_ts = db.execute(
        "SELECT vehicle_ts FROM observations WHERE trip_id='A'").fetchone()[0]
    assert vehicle_ts == "2026-07-18T21:51:30+00:00"


def test_vehicle_ts_never_overwritten_while_lat_lon_still_fill_independently(tmp_path):
    """Preserve the never-overwrite guarantee per column, not per row: a row
    that already has vehicle_ts (written by a later poller than the one that
    left lat/lon NULL) must keep its vehicle_ts untouched while still getting
    lat/lon filled from the same archived ping."""
    db = fresh_db()
    record_observation(db, "A", "2026-07-18", TS_UTC, "position", 1,
                       vehicle_ts="2026-07-18T21:50:00+00:00")
    archive = tmp_path / "archive"
    ts = int(dt.datetime(2026, 7, 18, 21, 51, 30, tzinfo=dt.timezone.utc).timestamp())
    write_snapshot(archive, "20260718", "215141", [vehicle("A", timestamp=ts)])
    backfill_archive(db, archive, apply=True)
    lat, lon, vts = db.execute(
        "SELECT lat, lon, vehicle_ts FROM observations WHERE trip_id='A'").fetchone()
    assert lat is not None and lon is not None
    assert vts == "2026-07-18T21:50:00+00:00"


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
