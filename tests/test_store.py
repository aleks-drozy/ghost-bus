import datetime as dt
import sqlite3

import pytest

from classify.store import init_store, record_heartbeat, record_observation, uptime

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 3, 23, 7, 0, tzinfo=UTC)


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    init_store(conn)
    return conn


def fill_heartbeats(db, start, minutes, ok=True, skip=()):
    for i in range(minutes):
        if i in skip:
            continue
        record_heartbeat(db, (start + dt.timedelta(minutes=i)).isoformat(), ok)


def test_full_heartbeats_give_uptime_1(db):
    fill_heartbeats(db, T0, 60)
    assert uptime(db, T0, T0 + dt.timedelta(hours=1)) == pytest.approx(1.0)


def test_half_missing_heartbeats(db):
    fill_heartbeats(db, T0, 60, skip=set(range(0, 60, 2)))
    assert uptime(db, T0, T0 + dt.timedelta(hours=1)) == pytest.approx(0.5)


def test_failed_polls_do_not_count(db):
    fill_heartbeats(db, T0, 60, ok=False)
    assert uptime(db, T0, T0 + dt.timedelta(hours=1)) == 0.0


def test_uptime_clipped_to_window(db):
    fill_heartbeats(db, T0 - dt.timedelta(hours=2), 240)  # covers well beyond
    assert uptime(db, T0, T0 + dt.timedelta(hours=1)) == pytest.approx(1.0, abs=0.02)


def test_observation_roundtrip(db):
    record_observation(db, "R1_wk_00", "2026-03-23", T0.isoformat(), "position", 3)
    rows = db.execute("SELECT trip_id, kind, stop_sequence FROM observations").fetchall()
    assert rows == [("R1_wk_00", "position", 3)]
