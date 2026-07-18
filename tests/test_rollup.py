import datetime as dt
import sqlite3

import pytest

from aggregate.rollup import route_day_rollup, route_hour_rollup

UTC = dt.timezone.utc


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
    CREATE TABLE trip_outcomes (
      trip_id TEXT, service_date TEXT, route_id TEXT, start_utc TEXT, outcome TEXT,
      PRIMARY KEY (trip_id, service_date));
    """)
    rows = [
        ("a", "2026-03-23", "R1", "2026-03-23T07:00:00+00:00", "COMPLETED"),
        ("b", "2026-03-23", "R1", "2026-03-23T07:30:00+00:00", "UNTRACKED"),
        ("c", "2026-03-23", "R1", "2026-03-23T08:00:00+00:00", "VANISHED"),
        ("d", "2026-03-23", "R1", "2026-03-23T08:30:00+00:00", "EXCLUDED"),
        ("e", "2026-03-23", "R1", "2026-03-23T09:00:00+00:00", "CANCELLED"),
        ("f", "2026-03-23", "R2", "2026-03-23T09:00:00+00:00", "EXCLUDED"),
    ]
    conn.executemany("INSERT INTO trip_outcomes VALUES (?,?,?,?,?)", rows)
    conn.commit()
    return conn


def test_route_day_counts_and_ghost_rate(db):
    rollup = route_day_rollup(db)
    r1 = next(r for r in rollup if r["route_id"] == "R1")
    assert r1["scheduled"] == 5 and r1["excluded"] == 1
    assert r1["ghost_rate"] == pytest.approx((1 + 1) / (5 - 1))


def test_all_excluded_route_has_null_rate(db):
    r2 = next(r for r in route_day_rollup(db) if r["route_id"] == "R2")
    assert r2["scheduled"] == 1 and r2["excluded"] == 1 and r2["ghost_rate"] is None


def test_counts_conserve_totals(db):
    for r in route_day_rollup(db):
        parts = r["excluded"] + r["cancelled"] + r["completed"] + r["vanished"] + r["untracked"]
        assert parts == r["scheduled"]


def test_hour_rollup_uses_local_hour(db):
    hours = {(r["route_id"], r["local_hour"]): r for r in route_hour_rollup(db)}
    assert ("R1", 7) in hours and hours[("R1", 7)]["scheduled"] == 2
