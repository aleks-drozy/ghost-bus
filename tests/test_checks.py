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


def test_conservation_and_vocabulary_accept_excluded_feed():
    # Amendment G3: EXCLUDED_FEED is a valid outcome and conservation sums
    # six classes. A five-class sum would report a real G3 database as
    # violating conservation and block every publish.
    db = make_db(GOOD + [("c", "2026-03-23", "R1",
                          "2026-03-23T08:00:00+00:00", "EXCLUDED_FEED")])
    assert check_outcomes_valid(db)["passed"]
    assert check_conservation(db)["passed"]
    assert check_rates_bounded(db)["passed"]


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


def test_rates_bounded_passes_on_real_rollup_rows():
    db = make_db(GOOD)
    result = check_rates_bounded(db)
    assert result["passed"] and result["violations"] == []


def test_rates_bounded_flags_out_of_range_vanished_rate(monkeypatch):
    import run_checks

    bad = {"route_id": "R1", "service_date": "2026-03-23", "scheduled": 10,
           "excluded": 0, "cancelled": 0, "completed": 8, "vanished": 1, "untracked": 1,
           "vanished_rate": 1.5, "vanished_lo": 0.0, "vanished_hi": 1.0,
           "untracked_rate": 0.1, "untracked_lo": 0.0, "untracked_hi": 0.4}
    monkeypatch.setattr(run_checks, "route_day_rollup", lambda db: [bad])
    result = run_checks.check_rates_bounded(None)
    assert not result["passed"] and result["violations"] == [bad]


def test_rates_bounded_flags_out_of_range_untracked_bound(monkeypatch):
    import run_checks

    bad = {"route_id": "R1", "service_date": "2026-03-23", "scheduled": 10,
           "excluded": 0, "cancelled": 0, "completed": 8, "vanished": 1, "untracked": 1,
           "vanished_rate": 0.1, "vanished_lo": 0.0, "vanished_hi": 0.4,
           "untracked_rate": 0.1, "untracked_lo": -0.2, "untracked_hi": 0.4}
    monkeypatch.setattr(run_checks, "route_day_rollup", lambda db: [bad])
    result = run_checks.check_rates_bounded(None)
    assert not result["passed"] and result["violations"] == [bad]


def test_rates_bounded_flags_point_estimate_outside_its_interval(monkeypatch):
    import run_checks

    bad = {"route_id": "R1", "service_date": "2026-03-23", "scheduled": 10,
           "excluded": 0, "cancelled": 0, "completed": 8, "vanished": 1, "untracked": 1,
           "vanished_rate": 0.9, "vanished_lo": 0.0, "vanished_hi": 0.4,
           "untracked_rate": 0.1, "untracked_lo": 0.0, "untracked_hi": 0.4}
    monkeypatch.setattr(run_checks, "route_day_rollup", lambda db: [bad])
    result = run_checks.check_rates_bounded(None)
    assert not result["passed"] and result["violations"] == [bad]


def test_rates_bounded_flags_partially_defined_rates(monkeypatch):
    import run_checks

    # All six rate fields share one denominator, so they are None together or
    # populated together. A half-populated row means the rollup is broken.
    bad = {"route_id": "R1", "service_date": "2026-03-23", "scheduled": 10,
           "excluded": 0, "cancelled": 0, "completed": 8, "vanished": 1, "untracked": 1,
           "vanished_rate": 0.1, "vanished_lo": 0.0, "vanished_hi": 0.4,
           "untracked_rate": None, "untracked_lo": None, "untracked_hi": None}
    monkeypatch.setattr(run_checks, "route_day_rollup", lambda db: [bad])
    result = run_checks.check_rates_bounded(None)
    assert not result["passed"] and result["violations"] == [bad]


def test_rates_bounded_passes_when_all_rates_are_null(monkeypatch):
    import run_checks

    row = {"route_id": "R2", "service_date": "2026-03-23", "scheduled": 1,
           "excluded": 1, "cancelled": 0, "completed": 0, "vanished": 0, "untracked": 0,
           "vanished_rate": None, "vanished_lo": None, "vanished_hi": None,
           "untracked_rate": None, "untracked_lo": None, "untracked_hi": None}
    monkeypatch.setattr(run_checks, "route_day_rollup", lambda db: [row])
    assert run_checks.check_rates_bounded(None)["passed"]
