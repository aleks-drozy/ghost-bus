import datetime as dt
import sqlite3

import pytest

from classify.run_classifier import run_for_dates
from classify.store import init_store, record_observation
from tests.fixtureville import build_gtfs_zip
from timetable.gtfs import load_gtfs

UTC = dt.timezone.utc


@pytest.fixture()
def db(tmp_path):
    conn = sqlite3.connect(":memory:")
    zip_path = tmp_path / "f.zip"
    build_gtfs_zip(zip_path)
    load_gtfs(zip_path, conn)
    init_store(conn)
    return conn


def _fill_heartbeats(db: sqlite3.Connection, start: dt.datetime, end: dt.datetime) -> None:
    rows = []
    ts = start
    while ts < end:
        rows.append((ts.isoformat(), 1))
        ts += dt.timedelta(minutes=1)
    db.executemany("INSERT OR REPLACE INTO heartbeats VALUES (?,?)", rows)
    db.commit()


def test_run_for_dates_classifies_and_scopes_by_agency(db):
    service_date = dt.date(2026, 3, 23)
    # Cover every FVB trip's window that day (07:00 through R1_late's ~01:45
    # next-day close) with heartbeats so uptime never drops below 90%.
    _fill_heartbeats(db, dt.datetime(2026, 3, 23, 6, 0, tzinfo=UTC),
                     dt.datetime(2026, 3, 24, 2, 0, tzinfo=UTC))
    # One vehicle observation near the end of R1_wk_00 -> COMPLETED.
    record_observation(db, "R1_wk_00", "2026-03-23",
                       dt.datetime(2026, 3, 23, 7, 55, tzinfo=UTC).isoformat(),
                       "position", 5)

    now_utc = dt.datetime(2026, 3, 24, 2, 0, tzinfo=UTC)  # every window that day has closed
    summary = run_for_dates(db, [service_date], agency_names={"Fixtureville Bus"},
                            now_utc=now_utc)

    counts = summary["2026-03-23"]
    assert sum(counts.values()) == 16  # 11 R1 + 5 R2 wk; R3 (Go-Ahead) excluded
    assert counts["COMPLETED"] == 1
    assert counts["UNTRACKED"] == 15

    # R3 was never even queried, let alone classified, by the agency filter.
    (r3_rows,) = db.execute(
        "SELECT COUNT(*) FROM trip_outcomes WHERE route_id='R3'").fetchone()
    assert r3_rows == 0


def test_run_for_dates_none_agency_includes_every_operator(db):
    _fill_heartbeats(db, dt.datetime(2026, 3, 23, 6, 0, tzinfo=UTC),
                     dt.datetime(2026, 3, 24, 2, 0, tzinfo=UTC))
    now_utc = dt.datetime(2026, 3, 24, 2, 0, tzinfo=UTC)
    summary = run_for_dates(db, [dt.date(2026, 3, 23)], agency_names=None, now_utc=now_utc)
    assert sum(summary["2026-03-23"].values()) == 18  # + 2 R3 (Go-Ahead) trips


def test_run_for_dates_handles_no_scheduled_trips(db):
    now_utc = dt.datetime(2026, 3, 23, 12, 0, tzinfo=UTC)
    # 2026-03-22 is a Sunday - Fixtureville runs no service that day.
    summary = run_for_dates(db, [dt.date(2026, 3, 22)], agency_names=None, now_utc=now_utc)
    assert summary["2026-03-22"] == {}


def test_run_for_dates_wires_feed_shields_into_classification(db, monkeypatch):
    # G3 wiring: run_for_dates must build the route->agency map, call
    # compute_shields, and thread both into classify_day - a shield that
    # exists but is never wired would silently re-enable accusations during
    # feed outages. Fixtureville's fleet (<= 18 active) sits below
    # MIN_ACTIVE_TRIPS by design, so arming is faked here; the real arming
    # math is covered in test_feedhealth.py.
    import classify.run_classifier as rc
    _fill_heartbeats(db, dt.datetime(2026, 3, 23, 6, 0, tzinfo=UTC),
                     dt.datetime(2026, 3, 24, 2, 0, tzinfo=UTC))

    seen = {}

    def fake_shields(db_, trips, agency_of_route):
        seen["agency_map"] = dict(agency_of_route)
        whole_day = (dt.datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
                     dt.datetime(2026, 3, 24, 6, 0, tzinfo=UTC))
        return {agency: [whole_day] for agency in set(agency_of_route.values())}

    monkeypatch.setattr(rc, "compute_shields", fake_shields)
    now_utc = dt.datetime(2026, 3, 24, 2, 0, tzinfo=UTC)
    summary = run_for_dates(db, [dt.date(2026, 3, 23)],
                            agency_names={"Fixtureville Bus"}, now_utc=now_utc)
    counts = summary["2026-03-23"]
    assert counts.get("UNTRACKED", 0) == 0
    assert counts["EXCLUDED_FEED"] == 16  # every would-be-UNTRACKED trip shielded
    assert seen["agency_map"].get("R1") == "Fixtureville Bus"
    (stored,) = db.execute(
        "SELECT COUNT(*) FROM trip_outcomes WHERE outcome='EXCLUDED_FEED'").fetchone()
    assert stored == 16


def test_run_for_dates_multiple_dates_are_independent(db):
    _fill_heartbeats(db, dt.datetime(2026, 3, 27, 6, 0, tzinfo=UTC),
                     dt.datetime(2026, 3, 29, 2, 0, tzinfo=UTC))
    now_utc = dt.datetime(2026, 3, 29, 2, 0, tzinfo=UTC)
    summary = run_for_dates(db, [dt.date(2026, 3, 27), dt.date(2026, 3, 28)],
                            agency_names=None, now_utc=now_utc)
    assert set(summary.keys()) == {"2026-03-27", "2026-03-28"}
    assert sum(summary["2026-03-27"].values()) == 18  # Friday: full weekday service
    assert sum(summary["2026-03-28"].values()) == 1   # Saturday: R2_sat_00 only
