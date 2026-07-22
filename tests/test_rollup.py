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


def test_route_day_counts_and_split_rates(db):
    rollup = route_day_rollup(db)
    r1 = next(r for r in rollup if r["route_id"] == "R1")
    assert r1["scheduled"] == 5 and r1["excluded"] == 1
    assert r1["vanished"] == 1 and r1["untracked"] == 1
    # Denominator for both rates is scheduled - excluded = 4. The two rates are
    # reported separately and are never summed (design decision D1); the old
    # combined ghost_rate of 2/4 is gone and must not reappear.
    assert r1["vanished_rate"] == pytest.approx(1 / 4)
    assert r1["untracked_rate"] == pytest.approx(1 / 4)
    # Wilson 95% interval for 1/4, hand-computed - see tests/test_rates.py.
    assert r1["vanished_lo"] == pytest.approx(0.045586062644636216, rel=1e-12)
    assert r1["vanished_hi"] == pytest.approx(0.6993639475573634, rel=1e-12)
    assert r1["untracked_lo"] == pytest.approx(0.045586062644636216, rel=1e-12)
    assert r1["untracked_hi"] == pytest.approx(0.6993639475573634, rel=1e-12)
    assert "ghost_rate" not in r1


def test_all_excluded_route_has_null_rates(db):
    r2 = next(r for r in route_day_rollup(db) if r["route_id"] == "R2")
    assert r2["scheduled"] == 1 and r2["excluded"] == 1
    # Denominator is 0: every rate field is None, never 0.0, and never a mix.
    for key in ("vanished_rate", "vanished_lo", "vanished_hi",
                "untracked_rate", "untracked_lo", "untracked_hi"):
        assert r2[key] is None, key
    assert "ghost_rate" not in r2


def test_hour_rollup_carries_the_same_rate_keys(db):
    hours = {(r["route_id"], r["local_hour"]): r for r in route_hour_rollup(db)}
    row = hours[("R1", 8)]
    # 08:00 UTC VANISHED + 08:30 UTC EXCLUDED -> scheduled 2, excluded 1, denom 1.
    assert row["scheduled"] == 2 and row["excluded"] == 1
    assert row["vanished_rate"] == pytest.approx(1.0)
    assert row["untracked_rate"] == pytest.approx(0.0)
    assert "ghost_rate" not in row


def test_counts_conserve_totals(db):
    for r in route_day_rollup(db):
        parts = r["excluded"] + r["cancelled"] + r["completed"] + r["vanished"] + r["untracked"]
        assert parts == r["scheduled"]


def test_hour_rollup_uses_local_hour(db):
    hours = {(r["route_id"], r["local_hour"]): r for r in route_hour_rollup(db)}
    assert ("R1", 7) in hours and hours[("R1", 7)]["scheduled"] == 2


def test_excluded_feed_is_counted_and_removed_from_the_denominator(db):
    # Amendment G3: EXCLUDED_FEED trips (feed degraded - NTA's failure, not
    # ours and not the operator's) leave the denominator exactly like
    # EXCLUDED does. R1 gains two: denominator 5+2 scheduled - 1 excluded -
    # 2 excluded_feed = 4, so both rates are unchanged by the shield.
    db.executemany("INSERT INTO trip_outcomes VALUES (?,?,?,?,?)", [
        ("g", "2026-03-23", "R1", "2026-03-23T09:30:00+00:00", "EXCLUDED_FEED"),
        ("h", "2026-03-23", "R1", "2026-03-23T10:00:00+00:00", "EXCLUDED_FEED"),
    ])
    db.commit()
    r1 = next(r for r in route_day_rollup(db) if r["route_id"] == "R1")
    assert r1["scheduled"] == 7 and r1["excluded_feed"] == 2
    assert r1["vanished_rate"] == pytest.approx(1 / 4)
    assert r1["untracked_rate"] == pytest.approx(1 / 4)
    parts = (r1["excluded"] + r1["excluded_feed"] + r1["cancelled"]
             + r1["completed"] + r1["vanished"] + r1["untracked"])
    assert parts == r1["scheduled"]
