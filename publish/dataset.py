"""Publish the open dataset: SQLite -> data/daily, data/uptime, data/manifest.json.

Runs on the VM, daily, after the classifier. stdlib only. The site is built in
CI from these files and never from the database (spec D3), so whatever this
module writes is exactly what the public sees. This module never touches git:
committing and pushing is ops/publish.sh's job.
"""
from __future__ import annotations

import csv
import datetime as dt
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1
BASELINE_REQUIRED_DAYS = 14
LOCAL_TZ = "Europe/Dublin"
UTC = dt.timezone.utc

# A service day is credited with a flat 1440 expected minutes. On the two DST
# days the local day is really 1380 or 1500 minutes long. Holding the
# denominator fixed keeps the published series comparable; the cost is that on
# the spring-forward day we understate our own uptime, and on the fall-back day
# a clamped 1.000000 can mask up to 60 minutes of downtime. Both are stated on
# the about-data page rather than silently corrected.
EXPECTED_MINUTES_PER_DAY = 1440

UPTIME_COLUMNS = ("service_date", "expected_minutes", "ok_minutes", "uptime_fraction")


def _write_csv(path: Path, columns, rows) -> None:
    """Write a CSV with LF line endings so output is byte-identical on any host
    and git diffs of the published dataset stay clean."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row[column] for column in columns])


def local_today(tz: str = LOCAL_TZ) -> dt.date:
    return dt.datetime.now(ZoneInfo(tz)).date()


def day_bounds_utc(day: dt.date, tz: str = LOCAL_TZ) -> tuple[dt.datetime, dt.datetime]:
    """[start, end) in UTC for one local service day."""
    zone = ZoneInfo(tz)
    nxt = day + dt.timedelta(days=1)
    start = dt.datetime(day.year, day.month, day.day, tzinfo=zone)
    end = dt.datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def uptime_days(db: sqlite3.Connection, today: dt.date) -> list[dt.date]:
    """Every complete local service day from the first heartbeat to yesterday.

    Contiguous by construction: a day with no heartbeats at all is still
    published, as a zero row. A gap in our own coverage is a fact about us and
    is never omitted or interpolated.
    """
    row = db.execute("SELECT MIN(ts_utc) FROM heartbeats").fetchone()
    if row is None or row[0] is None:
        return []
    first = dt.datetime.fromisoformat(row[0]).astimezone(ZoneInfo(LOCAL_TZ)).date()
    last = today - dt.timedelta(days=1)
    if first > last:
        return []
    return [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]


def uptime_row(db: sqlite3.Connection, day: dt.date) -> dict:
    start, end = day_bounds_utc(day)
    # Distinct minute buckets, not raw rows - matches classify.store.uptime, so
    # a crash-loop cannot inflate the published figure.
    (ok_minutes,) = db.execute(
        "SELECT COUNT(DISTINCT substr(ts_utc,1,16)) FROM heartbeats "
        "WHERE ok=1 AND ts_utc>=? AND ts_utc<?",
        (start.isoformat(), end.isoformat())).fetchone()
    fraction = min(1.0, ok_minutes / EXPECTED_MINUTES_PER_DAY)
    return {"service_date": day.isoformat(),
            "expected_minutes": EXPECTED_MINUTES_PER_DAY,
            "ok_minutes": ok_minutes,
            "uptime_fraction": f"{fraction:.6f}"}


def write_uptime_csvs(db: sqlite3.Connection, data_dir, days) -> list[Path]:
    written = []
    for day in days:
        path = Path(data_dir) / "uptime" / f"{day.isoformat()}.csv"
        _write_csv(path, UPTIME_COLUMNS, [uptime_row(db, day)])
        written.append(path)
    return written
