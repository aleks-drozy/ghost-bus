import sqlite3
import subprocess
import sys

from run_checks import check_conservation, check_outcomes_valid, check_rates_bounded


def make_db(rows):
    db = sqlite3.connect(":memory:")
    db.executescript("""
    CREATE TABLE trip_outcomes (
      trip_id TEXT, service_date TEXT, route_id TEXT, start_utc TEXT, outcome TEXT,
      PRIMARY KEY (trip_id, service_date));
    """)
    db.executemany("INSERT INTO trip_outcomes VALUES (?,?,?,?,?)", rows)
    db.commit()
    return db


GOOD = [("a", "2026-03-23", "R1", "2026-03-23T07:00:00+00:00", "COMPLETED"),
        ("b", "2026-03-23", "R1", "2026-03-23T07:30:00+00:00", "UNTRACKED")]


def test_all_checks_pass_on_good_db():
    db = make_db(GOOD)
    assert check_conservation(db)["passed"]
    assert check_rates_bounded(db)["passed"]
    assert check_outcomes_valid(db)["passed"]


def test_invalid_outcome_fails(tmp_path):
    db = make_db(GOOD + [("z", "2026-03-23", "R1", "2026-03-23T08:00:00+00:00", "MAYBE")])
    assert not check_outcomes_valid(db)["passed"]

    # An unrecognized outcome must not just fail the in-process check - it must
    # also make the CLI exit 1 cleanly (no traceback), since check_conservation
    # and check_rates_bounded would KeyError on it if they ran.
    dbfile = tmp_path / "bad.db"
    file_db = sqlite3.connect(dbfile)
    file_db.executescript("""
    CREATE TABLE trip_outcomes (
      trip_id TEXT, service_date TEXT, route_id TEXT, start_utc TEXT, outcome TEXT,
      PRIMARY KEY (trip_id, service_date));
    """)
    file_db.executemany("INSERT INTO trip_outcomes VALUES (?,?,?,?,?)",
                        GOOD + [("z", "2026-03-23", "R1", "2026-03-23T08:00:00+00:00", "MAYBE")])
    file_db.commit(); file_db.close()
    proc = subprocess.run([sys.executable, "run_checks.py", str(dbfile)],
                          capture_output=True, text=True)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr


def test_cli_exit_codes(tmp_path):
    # empty db file -> checks run on empty tables -> pass, exit 0
    dbfile = tmp_path / "s.db"
    db = sqlite3.connect(dbfile)
    db.executescript("""
    CREATE TABLE trip_outcomes (
      trip_id TEXT, service_date TEXT, route_id TEXT, start_utc TEXT, outcome TEXT,
      PRIMARY KEY (trip_id, service_date));
    """)
    db.commit(); db.close()
    proc = subprocess.run([sys.executable, "run_checks.py", str(dbfile)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
