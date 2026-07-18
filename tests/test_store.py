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


def test_window_filter_actually_excludes_outside_heartbeats(db):
    # Heartbeats cover only the FIRST HALF of the window (true uptime 0.5), plus
    # a flood of heartbeats outside the window on both sides. A broken window
    # filter would count the flood and saturate to 1.0 - this test pins 0.5.
    fill_heartbeats(db, T0, 30)                           # inside: minutes 0-29
    fill_heartbeats(db, T0 - dt.timedelta(hours=2), 120)  # entirely before window
    fill_heartbeats(db, T0 + dt.timedelta(hours=1), 120)  # entirely after window
    assert uptime(db, T0, T0 + dt.timedelta(hours=1)) == pytest.approx(0.5)


def test_invalid_observation_kind_raises(db):
    with pytest.raises(ValueError):
        record_observation(db, "T1", "2026-03-23", T0.isoformat(), "teleport", 1)


def test_empty_window_uptime_is_zero(db):
    fill_heartbeats(db, T0, 10)
    assert uptime(db, T0, T0) == 0.0
