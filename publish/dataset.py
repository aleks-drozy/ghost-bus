"""Publish the open dataset: SQLite -> data/daily, data/uptime, data/manifest.json.

Runs on the VM, daily, after the classifier. stdlib only. The site is built in
CI from these files and never from the database (spec D3), so whatever this
module writes is exactly what the public sees. This module never touches git:
committing and pushing is ops/publish.sh's job.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from aggregate.rollup import route_day_rollup
from ghostbus_config import get_db, read_agency_names
from publish.slugs import slug_map
from run_checks import check_conservation, check_outcomes_valid, check_rates_bounded


class GateFailed(Exception):
    """The publish gate did not pass, so nothing at all was written."""

SCHEMA_VERSION = 1
BASELINE_REQUIRED_DAYS = 14
LOCAL_TZ = "Europe/Dublin"
UTC = dt.timezone.utc

# Complete service days we refuse to publish, with the public reason.
# Deliberately a code constant, not configuration: withdrawing (or
# reinstating) a day changes what the public record claims, so it must be a
# commit, in the open, like a spec amendment. Withdrawn days publish no
# daily CSV, count for nothing (including the 14-day baseline), and are
# listed with their reason in the manifest and on the about-data page.
# Uptime is exempt: our own uptime is a fact about us, not an accusation.
WITHDRAWN_DAYS: dict[str, str] = {
    "2026-07-21": (
        "NTA VehiclePositions feed partially collapsed ~19:20-20:00 UTC "
        "(Dublin Bus and Bus Eireann reporting fell ~80% simultaneously while "
        "the tracker itself was healthy), mass-producing VANISHED verdicts "
        "that are feed artifacts, not operator behaviour. Withdrawn rather "
        "than caveated - see the methodology page, amendment G3."),
}

UPTIME_COLUMNS = ("service_date", "expected_minutes", "ok_minutes", "uptime_fraction")

DAILY_COLUMNS = ("service_date", "route_id", "route_short_name", "route_long_name",
                 "agency_name", "scheduled", "excluded", "excluded_feed",
                 "cancelled", "completed", "vanished", "untracked",
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


def _prune_orphans(directory: Path, wanted_stems: set[str]) -> None:
    """Delete every `daily/*.csv` (or `uptime/*.csv`) whose date is not in
    `wanted_stems`.

    Without this, a day that drops out of `days` - a DB restore, a VM
    rebuild, or an operator deleting bad outcome rows per RUNBOOK 8.4 - keeps
    its CSV sitting on disk forever, republished untouched by every later run.
    build_site reads every file in the directory (spec D3), so that orphan
    keeps entering the published window even though the manifest this same
    run writes no longer claims that day as coverage: the site would state a
    coverage the manifest it was built from denies. ops/publish.sh's
    fetch+reset restores these files from the remote every night, so without
    this prune the bug is self-perpetuating.
    """
    if not directory.is_dir():
        return
    for path in directory.glob("*.csv"):
        if path.stem not in wanted_stems:
            path.unlink()


def write_uptime_csvs(db: sqlite3.Connection, data_dir, days) -> list[Path]:
    directory = Path(data_dir) / "uptime"
    _prune_orphans(directory, {day.isoformat() for day in days})
    written = []
    for day in days:
        path = directory / f"{day.isoformat()}.csv"
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
    partial day understates trip counts and distorts every rate built on it.
    Withdrawn days (module constant above) are removed here, at the single
    source every publish decision reads, so they publish nothing and count
    for nothing downstream.

    Days before the tracker's first heartbeat are removed too: the
    classifier's first-ever run back-fills "yesterday", writing a full day
    of EXCLUDED verdicts for a service day the tracker did not exist on.
    Counting that day made coverage claim a first day the uptime record
    (which starts at the first heartbeat, see uptime_days) denies - two
    public artifacts disagreeing about when the record began. A day we
    never watched is not part of the record; with no heartbeats at all
    there is no record yet.
    """
    row = db.execute("SELECT MIN(ts_utc) FROM heartbeats").fetchone()
    if row is None or row[0] is None:
        return []
    first = dt.datetime.fromisoformat(row[0]).astimezone(ZoneInfo(LOCAL_TZ)).date().isoformat()
    return [d for (d,) in db.execute(
        "SELECT DISTINCT service_date FROM trip_outcomes "
        "WHERE service_date < ? ORDER BY service_date", (today.isoformat(),))
        if d not in WITHDRAWN_DAYS and d >= first]


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
        "excluded_feed": r["excluded_feed"],
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
    """One day's publishable rows. Convenience for a SINGLE day - tests, or a
    one-off inspection.

    NEVER call this in a loop over days. Despite the per-day signature it runs a
    full route_day_rollup every time, so looping is quadratic in published
    history and is exactly the nightly-run failure write_daily_csvs exists to
    avoid. For more than one day, call daily_rows_by_date once and index it.
    """
    return daily_rows_by_date(db, names).get(service_date, [])


def write_daily_csvs(db: sqlite3.Connection, data_dir, days,
                     names: dict) -> list[Path]:
    by_date = daily_rows_by_date(db, names)
    directory = Path(data_dir) / "daily"
    _prune_orphans(directory, set(days))
    written = []
    for day in days:
        path = directory / f"{day}.csv"
        _write_csv(path, DAILY_COLUMNS, by_date.get(day, []))
        written.append(path)
    return written


def run_gate(db: sqlite3.Connection) -> dict[str, bool]:
    """The publish gate, in the same order run_checks.main uses.

    outcomes_valid runs first and short-circuits: conservation and
    rates_bounded key into per-outcome dict slots, so an unrecognized outcome
    string would KeyError there instead of failing cleanly. The two are
    reported as False in that case, which never reaches the manifest - a failed
    gate writes nothing at all (see write_dataset).
    """
    if not check_outcomes_valid(db)["passed"]:
        return {"conservation": False, "rates_bounded": False,
                "outcomes_valid": False}
    return {"conservation": check_conservation(db)["passed"],
            "rates_bounded": check_rates_bounded(db)["passed"],
            "outcomes_valid": True}


def _count(db: sqlite3.Connection, sql: str) -> int:
    try:
        (n,) = db.execute(sql).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0
        raise
    return n


def _meta(db: sqlite3.Connection, key: str) -> str:
    try:
        row = db.execute("SELECT value FROM gtfs_meta WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return ""
        raise
    return row[0] if row and row[0] else ""


def timetable_hash(db: sqlite3.Connection) -> str:
    return _meta(db, "gtfs_hash")


def timetable_loaded_at(db: sqlite3.Connection) -> str:
    """When the current timetable was loaded, or "" if we never recorded it.

    Databases loaded before load_gtfs started writing this key report "", which
    the about-data page renders as an em dash. An absent fact is shown as
    unknown; it is never back-filled with a guess.
    """
    return _meta(db, "gtfs_loaded_at")


def published_route_ids(db: sqlite3.Connection, days: list[str]) -> list[str]:
    """Every route id appearing in the published service days.

    The BETWEEN range can span a withdrawn middle day, so a route seen only
    on a withdrawn day still enters route_slugs. Deliberate: slugs are URL
    reservations carrying no outcome data, and reserving early keeps a
    route's public URL stable for when it next appears on a published day.
    Verdict data cannot leak this way - the withdrawn day has no daily CSV,
    and every page is built from the CSVs alone.
    """
    if not days:
        return []
    # A range, not an IN list: `days` grows by one per day forever and would
    # eventually blow past SQLite's bound-parameter limit.
    return sorted({r for (r,) in db.execute(
        "SELECT DISTINCT route_id FROM trip_outcomes "
        "WHERE service_date BETWEEN ? AND ?", (days[0], days[-1]))})


def read_published_slugs(data_dir) -> dict[str, str]:
    """The route_slugs map from the manifest we published last time.

    The map lives in the dataset rather than beside the previous site build
    because the site is rebuilt from scratch on an ephemeral CI runner every
    run: a map kept in the site output would always read back empty, and a
    route page's public URL could move whenever route ids changed. data/ is a
    working copy of what is already public, so this file is the real previous
    map. A missing or unreadable manifest means "nothing published yet".
    """
    path = Path(data_dir) / "manifest.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("route_slugs") or {}
    except (ValueError, OSError):
        return {}


def published_slugs(route_ids, previous: dict[str, str]) -> dict[str, str]:
    """The map to publish: every current route, plus every route ever published.

    Retired route ids are fed back through slug_map alongside the live ones, so
    they keep the slug they were published under and go on reserving it. A link
    to a route that has since been withdrawn therefore still resolves to that
    route, and can never be silently handed to a different one. slug_map's own
    rule is unchanged - on its own it drops ids it was not asked about, which is
    exactly why the carry-forward is done here and not there.
    """
    return slug_map(set(route_ids) | set(previous), existing=previous)


def build_manifest(db: sqlite3.Connection, days: list[str], gate: dict,
                   names: dict, slugs: dict, now_utc: dt.datetime) -> dict:
    """The machine-readable description of this release. Pure: writes nothing."""
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_utc.astimezone(UTC).replace(microsecond=0).isoformat(),
        "timetable_hash": timetable_hash(db),
        "timetable_loaded_at": timetable_loaded_at(db),
        "coverage": {"first_day": days[0] if days else None,
                     "last_day": days[-1] if days else None,
                     "complete_days": len(days)},
        "scoreboard_ready": len(days) >= BASELINE_REQUIRED_DAYS,
        "baseline_required_days": BASELINE_REQUIRED_DAYS,
        # Days we refuse to publish, each with its public reason (see
        # WITHDRAWN_DAYS above). Sorted for byte-stable output.
        "withdrawn_days": [
            {"service_date": day, "reason": reason}
            for day, reason in sorted(WITHDRAWN_DAYS.items())],
        "gate": {"conservation": gate["conservation"],
                 "rates_bounded": gate["rates_bounded"],
                 "outcomes_valid": gate["outcomes_valid"]},
        # The configured operator allow-list, not something derived from the
        # feed: scheduled_trips (timetable/gtfs.py) only ever schedules trips
        # for these agencies in the first place, so "every scheduled trip"
        # elsewhere on the site means every trip of THESE operators, not every
        # bus in the country. Published here so about-data.html can say so.
        "agencies": sorted(read_agency_names()),
        # The poller archives exactly one snapshot per successful poll, and
        # writes an ok=1 heartbeat in the same step, so ok heartbeats are the
        # snapshot count without walking the archive directory.
        "counts": {"observations": _count(db, "SELECT COUNT(*) FROM observations"),
                   "snapshots": _count(db, "SELECT COUNT(*) FROM heartbeats WHERE ok=1"),
                   "trips_classified": _count(db, "SELECT COUNT(*) FROM trip_outcomes")},
        "unnamed_routes": unnamed_routes(db, names),
        # Published here, not in the site output: CI checks this file out and
        # rebuilds _site from scratch every run, so this is the only copy that
        # survives to keep route URLs where they are.
        "route_slugs": dict(slugs),
    }


def write_manifest(data_dir, manifest: dict) -> Path:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
    return path


def write_dataset(db: sqlite3.Connection, data_dir, *,
                  today: dt.date | None = None,
                  now_utc: dt.datetime | None = None) -> dict:
    """Write the whole published dataset and return the manifest describing it."""
    data_dir = Path(data_dir)
    today = local_today() if today is None else today
    now_utc = dt.datetime.now(UTC) if now_utc is None else now_utc
    # The gate runs before the first mkdir: a failed gate must leave the
    # previously published dataset in place, untouched, rather than replace it
    # with numbers nothing has verified.
    gate = run_gate(db)
    if not all(gate.values()):
        failed = ", ".join(sorted(k for k, ok in gate.items() if not ok))
        raise GateFailed(failed)
    names = route_names(db)
    days = complete_service_days(db, today)
    # Read the previous map BEFORE write_manifest overwrites it below.
    slugs = published_slugs(published_route_ids(db, days),
                            read_published_slugs(data_dir))

    # Uptime is deliberately exempt from the 14-day baseline gate (spec D6): it
    # is our own downtime, not a claim about any operator, and the site's
    # pre-baseline mode depends on it being published from day one.
    write_uptime_csvs(db, data_dir, uptime_days(db, today))

    daily_dir = data_dir / "daily"
    if len(days) >= BASELINE_REQUIRED_DAYS:
        write_daily_csvs(db, data_dir, days, names)
    elif daily_dir.is_dir():
        # The baseline gate is a state, not an event: if coverage falls back
        # below it, previously published route data is WITHDRAWN, not left
        # standing next to a page saying we publish nothing about any route.
        shutil.rmtree(daily_dir)

    manifest = build_manifest(db, days, gate, names, slugs, now_utc)
    write_manifest(data_dir, manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    """Write the dataset, or write nothing and exit 1.

    This CLI never invokes git. ops/publish.sh commits and pushes; keeping the
    two apart is what lets a gate failure stop the run before any repository is
    touched at all.
    """
    parser = argparse.ArgumentParser(
        description="Publish the Ghost Bus dataset from SQLite to CSV + manifest.")
    parser.add_argument("--db", default="state/ghostbus.db",
                        help="path to the SQLite database (default: state/ghostbus.db)")
    parser.add_argument("--data-dir", default="data",
                        help="directory to write into (default: data)")
    args = parser.parse_args(argv)
    db = get_db(args.db)
    try:
        manifest = write_dataset(db, Path(args.data_dir))
    except GateFailed as exc:
        print(f"FAIL publish gate: {exc}", file=sys.stderr)
        print("wrote nothing", file=sys.stderr)
        return 1
    print(f"published {manifest['coverage']['complete_days']} complete days, "
          f"scoreboard_ready={manifest['scoreboard_ready']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
