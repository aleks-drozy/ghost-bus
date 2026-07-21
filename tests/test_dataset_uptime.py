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


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_published_dataset_paths_are_not_git_ignored():
    """An ignored data/ makes the whole publish pipeline a silent no-op.

    `git add -- data` would stage nothing, `git diff --cached --quiet` would
    exit 0, and the publisher would print "dataset unchanged, nothing to push"
    every night while publishing nothing at all.
    """
    for path in ("data/manifest.json", "data/daily/2026-03-23.csv",
                 "data/uptime/2026-03-23.csv"):
        proc = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO)
        assert proc.returncode == 1, f"{path} is git-ignored"
    # The probe captures stay ignored - they are binary fixtures, not output.
    proc = subprocess.run(["git", "check-ignore", "-q", "data/probe/vehicles.pb"],
                          cwd=REPO)
    assert proc.returncode == 0, "data/probe/ must stay ignored"
