"""Classifier production entry point.

Classifies scheduled trips for today and yesterday (Europe/Dublin local
dates) - yesterday catches trips whose window only closed after local
midnight (e.g. a past-midnight run) that today's date wouldn't have covered
by itself. Idempotent: classify_day upserts by (trip_id, service_date), so
reruns never double-count.

Runnable as: python -m classify.run_classifier
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo

from classify.outcomes import classify_day
from classify.store import init_store
from ghostbus_config import get_db, read_agency_names, read_match_radius_m
from timetable.gtfs import scheduled_trips

TZ = "Europe/Dublin"


def run_for_dates(db: sqlite3.Connection, dates: list[dt.date],
                  agency_names: set[str] | None,
                  now_utc: dt.datetime, radius_m: float = 250.0) -> dict[str, dict[str, int]]:
    """Classify each service date's scheduled trips; return per-date outcome counts."""
    summary: dict[str, dict[str, int]] = {}
    for service_date in dates:
        trips = scheduled_trips(db, service_date, agency_names=agency_names)
        outcomes = classify_day(db, trips, now_utc, radius_m)
        counts: dict[str, int] = {}
        for outcome in outcomes.values():
            counts[outcome] = counts.get(outcome, 0) + 1
        summary[str(service_date)] = counts
    return summary


def main() -> int:  # pragma: no cover
    db = get_db()
    init_store(db)
    agency_names = read_agency_names()
    radius_m = read_match_radius_m()
    now_utc = dt.datetime.now(dt.timezone.utc)
    today_local = now_utc.astimezone(ZoneInfo(TZ)).date()
    dates = [today_local, today_local - dt.timedelta(days=1)]
    summary = run_for_dates(db, dates, agency_names, now_utc, radius_m)
    for service_date, counts in summary.items():
        print(service_date, counts)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
