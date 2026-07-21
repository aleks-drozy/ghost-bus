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

from aggregate.rollup import route_day_rollup

SCHEMA_VERSION = 1
BASELINE_REQUIRED_DAYS = 14
LOCAL_TZ = "Europe/Dublin"
UTC = dt.timezone.utc

UPTIME_COLUMNS = ("service_date", "expected_minutes", "ok_minutes", "uptime_fraction")

DAILY_COLUMNS = ("service_date", "route_id", "route_short_name", "route_long_name",
                 "agency_name", "scheduled", "excluded", "cancelled", "completed",
                 "vanished", "untracked",
                 "vanished_rate", "vanished_lo", "vanished_hi",
                 "untracked_rate", "untracked_lo", "untracked_hi")


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


def expected_minutes(day: dt.date, tz: str = LOCAL_TZ) -> int:
    """The true length of one local service day, in minutes.

    Derived from day_bounds_utc rather than assumed flat: on the two DST
    transition days the local day is really 1380 or 1500 minutes, not 1440. A
    flat denominator would understate uptime on the short day, and - the
    error that matters - would let min(1.0, ...) clamp on the long day and
    silently hide up to 60 minutes of real downtime. Uptime exists to hold
    the tracker itself accountable, so it must never be the one number able
    to hide the tracker's own outage.
    """
    start, end = day_bounds_utc(day, tz)
    return int((end - start).total_seconds() / 60)


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
    expected = expected_minutes(day)
    # Distinct minute buckets, not raw rows - matches classify.store.uptime, so
    # a crash-loop cannot inflate the published figure.
    (ok_minutes,) = db.execute(
        "SELECT COUNT(DISTINCT substr(ts_utc,1,16)) FROM heartbeats "
        "WHERE ok=1 AND ts_utc>=? AND ts_utc<?",
        (start.isoformat(), end.isoformat())).fetchone()
    # min() now only guards a genuinely impossible over-count - with a correct
    # per-day denominator it can no longer mask a real hour of downtime.
    fraction = min(1.0, ok_minutes / expected)
    return {"service_date": day.isoformat(),
            "expected_minutes": expected,
            "ok_minutes": ok_minutes,
            "uptime_fraction": f"{fraction:.6f}"}


def write_uptime_csvs(db: sqlite3.Connection, data_dir, days) -> list[Path]:
    written = []
    for day in days:
        path = Path(data_dir) / "uptime" / f"{day.isoformat()}.csv"
        _write_csv(path, UPTIME_COLUMNS, [uptime_row(db, day)])
        written.append(path)
    return written


def _fmt_rate(value: float | None) -> str:
    """An undefined rate is published as an empty cell, never as 0.0."""
    return "" if value is None else f"{value:.6f}"


def route_names(db: sqlite3.Connection) -> dict[str, tuple[str, str, str]]:
    """route_id -> (short name, long name, agency name), all non-null strings."""
    try:
        rows = db.execute(
            "SELECT r.route_id, COALESCE(r.route_short_name,''), "
            "COALESCE(r.route_long_name,''), COALESCE(a.agency_name,'') "
            "FROM gtfs_routes r LEFT JOIN gtfs_agency a ON a.agency_id = r.agency_id"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        # A database predating the timetable load simply has no names yet; every
        # route then lands in unnamed_routes, which is published. Anything else
        # (I/O error, corruption) must crash rather than quietly blank the names.
        if "no such table" in str(exc):
            return {}
        raise
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def unnamed_routes(db: sqlite3.Connection, names: dict) -> list[str]:
    """Route ids in trip_outcomes with no gtfs_routes row - surfaced, not dropped."""
    seen = {r for (r,) in db.execute("SELECT DISTINCT route_id FROM trip_outcomes")}
    return sorted(seen - set(names))


def complete_service_days(db: sqlite3.Connection, today: dt.date) -> list[str]:
    """Spec D7: only service days strictly before today (Europe/Dublin). A
    partial day understates trip counts and distorts every rate built on it."""
    return [d for (d,) in db.execute(
        "SELECT DISTINCT service_date FROM trip_outcomes "
        "WHERE service_date < ? ORDER BY service_date", (today.isoformat(),))]


def _daily_row(r: dict, names: dict) -> dict:
    short, long_name, agency = names.get(r["route_id"], ("", "", ""))
    return {
        "service_date": r["service_date"],
        "route_id": r["route_id"],
        "route_short_name": short,
        "route_long_name": long_name,
        "agency_name": agency,
        "scheduled": r["scheduled"],
        "excluded": r["excluded"],
        "cancelled": r["cancelled"],
        "completed": r["completed"],
        "vanished": r["vanished"],
        "untracked": r["untracked"],
        "vanished_rate": _fmt_rate(r["vanished_rate"]),
        "vanished_lo": _fmt_rate(r["vanished_lo"]),
        "vanished_hi": _fmt_rate(r["vanished_hi"]),
        "untracked_rate": _fmt_rate(r["untracked_rate"]),
        "untracked_lo": _fmt_rate(r["untracked_lo"]),
        "untracked_hi": _fmt_rate(r["untracked_hi"]),
    }


def daily_rows_by_date(db: sqlite3.Connection, names: dict) -> dict[str, list[dict]]:
    """Every publishable row, bucketed by service_date, from ONE full rollup.

    route_day_rollup materialises the whole trip_outcomes table, so it is called
    exactly once here and the result is indexed. Calling it per day would make
    the nightly run quadratic in published history.
    """
    by_date: dict[str, list[dict]] = {}
    for r in route_day_rollup(db):
        by_date.setdefault(r["service_date"], []).append(_daily_row(r, names))
    for rows in by_date.values():
        # Explicit, so row order is this module's own guarantee rather than an
        # inherited property of the rollup's internal sort.
        rows.sort(key=lambda row: row["route_id"])
    return by_date


def daily_rows(db: sqlite3.Connection, service_date: str, names: dict) -> list[dict]:
    return daily_rows_by_date(db, names).get(service_date, [])


def write_daily_csvs(db: sqlite3.Connection, data_dir, days,
                     names: dict) -> list[Path]:
    by_date = daily_rows_by_date(db, names)
    written = []
    for day in days:
        path = Path(data_dir) / "daily" / f"{day}.csv"
        _write_csv(path, DAILY_COLUMNS, by_date.get(day, []))
        written.append(path)
    return written
