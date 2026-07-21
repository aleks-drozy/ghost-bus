import datetime as dt
import sqlite3

from publish.dataset import run_gate, timetable_hash, timetable_loaded_at
from tests.dataset_fixture import GTFS_HASH, GTFS_LOADED_AT, build_db
from tests.fixtureville import build_gtfs_zip
from timetable.gtfs import load_gtfs


def test_run_gate_reports_all_three_checks():
    db = build_db()
    assert run_gate(db) == {"conservation": True, "rates_bounded": True,
                            "outcomes_valid": True}


def test_run_gate_short_circuits_on_an_invalid_outcome():
    # check_conservation and check_rates_bounded would KeyError on an unknown
    # outcome, so an invalid-outcome database must never reach them.
    db = build_db()
    db.execute("INSERT INTO trip_outcomes VALUES "
               "('bad','2026-03-23','R1','2026-03-23T20:00:00+00:00','MAYBE')")
    db.commit()
    assert run_gate(db) == {"conservation": False, "rates_bounded": False,
                            "outcomes_valid": False}


def test_timetable_hash_reads_gtfs_meta():
    assert timetable_hash(build_db()) == GTFS_HASH


def test_timetable_hash_missing_is_an_empty_string():
    db = build_db()
    db.execute("DELETE FROM gtfs_meta")
    db.commit()
    assert timetable_hash(db) == ""


def test_provenance_survives_a_database_with_no_gtfs_meta_table():
    db = sqlite3.connect(":memory:")
    assert timetable_hash(db) == ""
    assert timetable_loaded_at(db) == ""


def test_timetable_loaded_at_reads_gtfs_meta():
    assert timetable_loaded_at(build_db()) == GTFS_LOADED_AT


def test_timetable_loaded_at_missing_is_an_empty_string():
    # A database loaded before this key existed must degrade to "unknown",
    # never to a fabricated date.
    db = build_db()
    db.execute("DELETE FROM gtfs_meta WHERE key='gtfs_loaded_at'")
    db.commit()
    assert timetable_loaded_at(db) == ""


def test_load_gtfs_records_when_the_timetable_was_loaded(tmp_path):
    conn = sqlite3.connect(":memory:")
    zip_path = tmp_path / "f.zip"
    build_gtfs_zip(zip_path)
    load_gtfs(zip_path, conn)
    stamp = timetable_loaded_at(conn)
    parsed = dt.datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == dt.timedelta(0)
    assert parsed.microsecond == 0
