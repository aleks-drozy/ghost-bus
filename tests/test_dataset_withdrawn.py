"""Withdrawn days (decision with amendment G3, 2026-07-22).

A withdrawn day is a complete service day we refuse to publish, with the
reason stated in the manifest - 2026-07-21's VehiclePositions outage
mass-produced false VANISHED verdicts, and a caveat under a ranking table
protects nobody. Withdrawal is a module constant, not configuration:
changing it is a public act and must be a commit.
"""
import datetime as dt

import publish.dataset as dataset
from publish.dataset import (WITHDRAWN_DAYS, complete_service_days,
                             write_dataset)
from tests.dataset_fixture import build_db, consecutive_dates

UTC = dt.timezone.utc


def _full_heartbeats(days):
    rows = []
    for day in days:
        for m in range(0, 1440, 1):
            h, mi = divmod(m, 60)
            rows.append((f"{day}T{h:02d}:{mi:02d}:00+00:00", 1))
    return rows


def test_2026_07_21_is_withdrawn_with_a_feed_reason():
    # Pins Alex's 2026-07-22 decision: the feed-degradation day never
    # publishes. If this ever needs to change, it changes here, in public.
    assert "2026-07-21" in WITHDRAWN_DAYS
    assert "feed" in WITHDRAWN_DAYS["2026-07-21"].lower()


def test_withdrawn_day_leaves_complete_service_days(monkeypatch):
    days = consecutive_dates(3)
    db = build_db(days)
    monkeypatch.setattr(dataset, "WITHDRAWN_DAYS", {days[1]: "test reason"})
    today = dt.date.fromisoformat(days[-1]) + dt.timedelta(days=1)
    assert complete_service_days(db, today) == [days[0], days[2]]


def test_withdrawn_day_never_gets_a_csv_and_a_stale_one_is_pruned(tmp_path, monkeypatch):
    days = consecutive_dates(15)
    db = build_db(days, heartbeats=_full_heartbeats(days))
    today = dt.date.fromisoformat(days[-1]) + dt.timedelta(days=1)
    # First publish with nothing withdrawn: 15 days, all CSVs exist.
    write_dataset(db, tmp_path, today=today,
                  now_utc=dt.datetime(2026, 3, 18, 3, 0, tzinfo=UTC))
    assert (tmp_path / "daily" / f"{days[3]}.csv").is_file()
    # Withdraw one day and republish: its CSV is pruned, the others remain.
    monkeypatch.setattr(dataset, "WITHDRAWN_DAYS", {days[3]: "test feed event"})
    manifest = write_dataset(db, tmp_path, today=today,
                             now_utc=dt.datetime(2026, 3, 18, 3, 5, tzinfo=UTC))
    assert not (tmp_path / "daily" / f"{days[3]}.csv").exists()
    assert (tmp_path / "daily" / f"{days[2]}.csv").is_file()
    assert manifest["coverage"]["complete_days"] == 14
    assert manifest["withdrawn_days"] == [
        {"service_date": days[3], "reason": "test feed event"}]


def test_withdrawn_day_does_not_count_toward_the_baseline(tmp_path, monkeypatch):
    # 14 days minus 1 withdrawn = 13 complete: the scoreboard must NOT ready.
    days = consecutive_dates(14)
    db = build_db(days, heartbeats=_full_heartbeats(days))
    monkeypatch.setattr(dataset, "WITHDRAWN_DAYS", {days[0]: "test feed event"})
    today = dt.date.fromisoformat(days[-1]) + dt.timedelta(days=1)
    manifest = write_dataset(db, tmp_path, today=today,
                             now_utc=dt.datetime(2026, 3, 17, 3, 0, tzinfo=UTC))
    assert manifest["coverage"]["complete_days"] == 13
    assert manifest["scoreboard_ready"] is False
    assert not (tmp_path / "daily").exists()


def test_withdrawal_does_not_touch_uptime(tmp_path, monkeypatch):
    # Our own uptime is not an accusation; the tracker ran fine on the
    # withdrawn day and its uptime row still publishes.
    days = consecutive_dates(3)
    db = build_db(days, heartbeats=_full_heartbeats(days))
    monkeypatch.setattr(dataset, "WITHDRAWN_DAYS", {days[1]: "test feed event"})
    today = dt.date.fromisoformat(days[-1]) + dt.timedelta(days=1)
    write_dataset(db, tmp_path, today=today,
                  now_utc=dt.datetime(2026, 3, 6, 3, 0, tzinfo=UTC))
    assert (tmp_path / "uptime" / f"{days[1]}.csv").is_file()
