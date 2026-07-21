import datetime as dt
import shutil
import subprocess
from pathlib import Path

import pytest

from publish.dataset import (UPTIME_COLUMNS, day_bounds_utc, local_today,
                             uptime_days, uptime_row, write_uptime_csvs)
from tests.dataset_fixture import build_db

UTC = dt.timezone.utc
REPO = Path(__file__).resolve().parents[1]

GOLDEN_UPTIME = (
    "service_date,expected_minutes,ok_minutes,uptime_fraction\n"
    "2026-03-23,1440,2,0.001389\n"
)


def test_uptime_columns_match_the_spec():
    assert UPTIME_COLUMNS == ("service_date", "expected_minutes", "ok_minutes",
                              "uptime_fraction")


def test_golden_uptime_csv(tmp_path):
    db = build_db()
    days = uptime_days(db, dt.date(2026, 3, 24))
    assert days == [dt.date(2026, 3, 23)]
    written = write_uptime_csvs(db, tmp_path, days)
    assert written == [tmp_path / "uptime" / "2026-03-23.csv"]
    assert written[0].read_bytes() == GOLDEN_UPTIME.encode("utf-8")


def test_duplicate_heartbeats_in_one_minute_count_once():
    # Three ok heartbeats, two of them in the 00:00 bucket -> 2 ok minutes.
    db = build_db()
    assert uptime_row(db, dt.date(2026, 3, 23))["ok_minutes"] == 2


def test_failed_poll_is_not_an_ok_minute():
    db = build_db(heartbeats=[("2026-03-23T00:00:00.100000+00:00", 0)])
    assert uptime_row(db, dt.date(2026, 3, 23))["ok_minutes"] == 0


def test_day_with_no_heartbeats_is_written_as_a_visible_zero(tmp_path):
    # A gap must be published as a zero row, never interpolated or omitted.
    db = build_db()
    days = uptime_days(db, dt.date(2026, 3, 25))
    assert days == [dt.date(2026, 3, 23), dt.date(2026, 3, 24)]
    write_uptime_csvs(db, tmp_path, days)
    gap = (tmp_path / "uptime" / "2026-03-24.csv").read_text(encoding="utf-8")
    assert gap.splitlines()[1] == "2026-03-24,1440,0,0.000000"


def test_write_uptime_csvs_prunes_a_csv_whose_day_dropped_out_of_coverage(tmp_path):
    """C1's uptime mirror of the daily-csv prune: an uptime CSV whose day is
    no longer in `days` must be removed, not left behind for build_site's
    directory scan to read back."""
    db = build_db()
    day1, day2 = dt.date(2026, 3, 23), dt.date(2026, 3, 24)
    write_uptime_csvs(db, tmp_path, [day1, day2])
    assert sorted(p.name for p in (tmp_path / "uptime").iterdir()) == \
        ["2026-03-23.csv", "2026-03-24.csv"]

    write_uptime_csvs(db, tmp_path, [day1])
    assert sorted(p.name for p in (tmp_path / "uptime").iterdir()) == \
        ["2026-03-23.csv"]


def test_day_boundary_is_local_not_utc():
    # 23:30Z on 14 July is 00:30 local (Dublin is UTC+1 in summer), so the
    # heartbeat belongs to service day 2026-07-15.
    db = build_db(heartbeats=[("2026-07-14T23:30:00.000000+00:00", 1)])
    assert uptime_days(db, dt.date(2026, 7, 16)) == [dt.date(2026, 7, 15)]
    assert uptime_row(db, dt.date(2026, 7, 15))["ok_minutes"] == 1
    assert uptime_row(db, dt.date(2026, 7, 14))["ok_minutes"] == 0
    start, end = day_bounds_utc(dt.date(2026, 7, 15))
    assert start == dt.datetime(2026, 7, 14, 23, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 7, 15, 23, 0, tzinfo=UTC)


def test_empty_heartbeat_table_yields_no_days():
    db = build_db(heartbeats=[])
    assert uptime_days(db, dt.date(2026, 3, 24)) == []


def test_local_today_returns_a_date():
    assert isinstance(local_today(), dt.date)


def test_spring_forward_day_has_1380_expected_minutes_and_full_uptime():
    # 2027-03-28: Europe/Dublin loses an hour (confirmed via zoneinfo scan:
    # offset 0:00 -> 1:00 on this date), so the local day is really 1380
    # minutes. Full coverage must read as fraction 1.0, not the flat-1440
    # 1380/1440 = 0.958 that would understate a perfect day.
    day = dt.date(2027, 3, 28)
    start, end = day_bounds_utc(day)
    heartbeats = []
    ts = start
    while ts < end:
        heartbeats.append((ts.isoformat(), 1))
        ts += dt.timedelta(minutes=1)
    db = build_db(heartbeats=heartbeats)
    row = uptime_row(db, day)
    assert row["expected_minutes"] == 1380
    assert row["uptime_fraction"] == "1.000000"


def test_fall_back_day_has_1500_expected_minutes_and_reveals_masked_downtime():
    # 2027-10-31: Europe/Dublin gains an hour (confirmed via zoneinfo scan:
    # offset 1:00 -> 0:00 on this date), so the local day is really 1500
    # minutes. Exactly 1440 ok minutes - a full flat-1440 day's worth - still
    # leaves a real hour of downtime. A flat denominator would clamp this to
    # fraction 1.0 and hide that hour entirely; this is the case that matters.
    day = dt.date(2027, 10, 31)
    start, end = day_bounds_utc(day)
    heartbeats = []
    ts = start
    for _ in range(1440):
        heartbeats.append((ts.isoformat(), 1))
        ts += dt.timedelta(minutes=1)
    db = build_db(heartbeats=heartbeats)
    row = uptime_row(db, day)
    assert row["expected_minutes"] == 1500
    assert row["ok_minutes"] == 1440
    assert float(row["uptime_fraction"]) < 1.0


def test_ordinary_day_still_has_1440_expected_minutes():
    db = build_db()
    assert uptime_row(db, dt.date(2026, 3, 23))["expected_minutes"] == 1440


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_local_data_directory_is_git_ignored():
    """M1: Task 4 un-ignored data/ back when the published dataset was going
    to live in THIS repository. Task 18 moved the real dataset out to the
    separate `ghost-bus-data` repo (cloned by ops/publish.sh into
    data-repo/, itself gitignored below) and nothing re-ignored data/ here
    afterwards.

    Left un-ignored, one default-args `python -m publish.dataset` run from
    `/opt/ghost-bus` (no `--data-dir`, which is exactly what an operator
    debugging by hand would run) writes straight into this checkout's own
    data/, turns up as untracked files under `git status --porcelain`, and
    wedges ops/publish.sh's dirty-checkout guard every night after - the
    inverse of the failure this test's predecessor
    (test_published_dataset_paths_are_not_git_ignored, when data/ WAS the
    real publish target) used to guard against.
    """
    for path in ("data/manifest.json", "data/daily/2026-03-23.csv",
                 "data/uptime/2026-03-23.csv", "data/probe/vehicles.pb"):
        proc = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO)
        assert proc.returncode == 0, f"{path} must be git-ignored"
