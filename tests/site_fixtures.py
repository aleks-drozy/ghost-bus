"""Build a fake *published* dataset on disk for site-builder tests.

The site builder reads CSVs, never the database, so its tests need CSVs, not a
sqlite fixture. Everything here writes UTF-8 explicitly: this repo runs on
Windows where the default codec is cp1252.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

DAILY_COLUMNS = [
    "service_date", "route_id", "route_short_name", "route_long_name", "agency_name",
    "scheduled", "excluded", "cancelled", "completed", "vanished", "untracked",
    "vanished_rate", "vanished_lo", "vanished_hi",
    "untracked_rate", "untracked_lo", "untracked_hi",
]
UPTIME_COLUMNS = ["service_date", "expected_minutes", "ok_minutes", "uptime_fraction"]

DEFAULT_MANIFEST = {
    "schema_version": 1,
    "generated_at": "2026-07-20T04:00:00+00:00",
    "timetable_hash": "0f1c9a2b3d4e5f60",
    "timetable_loaded_at": "2026-07-01T02:00:00+00:00",
    "coverage": {"first_day": "2026-06-01", "last_day": "2026-06-28", "complete_days": 28},
    "scoreboard_ready": True,
    "baseline_required_days": 14,
    "gate": {"conservation": True, "rates_bounded": True, "outcomes_valid": True},
    "agencies": ["Dublin Bus", "Go-Ahead Ireland"],
    "counts": {"observations": 128400, "snapshots": 40320, "trips_classified": 9111},
    "unnamed_routes": [],
    # The published route-id -> slug map. Empty here so each test states the
    # map it cares about; the builder falls back to computing one for any route
    # id the dataset does not carry.
    "route_slugs": {},
}


def daily_row(service_date, route_id, **kw):
    """A daily CSV row with every column present. Counts default to 0, rates to ''."""
    row = {c: "" for c in DAILY_COLUMNS}
    row["service_date"] = service_date
    row["route_id"] = route_id
    for c in ("scheduled", "excluded", "cancelled", "completed", "vanished", "untracked"):
        row[c] = 0
    row.update(kw)
    return row


def uptime_row(service_date, expected_minutes=1440, ok_minutes=1440, uptime_fraction=None):
    if uptime_fraction is None:
        uptime_fraction = ok_minutes / expected_minutes if expected_minutes else ""
    return {
        "service_date": service_date,
        "expected_minutes": expected_minutes,
        "ok_minutes": ok_minutes,
        "uptime_fraction": uptime_fraction,
    }


def _write_csv(path, columns, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_dataset(root, daily_rows=(), uptime_rows=(), manifest=None):
    """Write a data/ tree and return its Path.

    daily/ is created only when there are daily rows: a pre-baseline dataset
    has no daily directory at all, and the builder refuses to render a
    'we publish nothing about any route' page next to one.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    man = json.loads(json.dumps(DEFAULT_MANIFEST))
    if manifest:
        man.update(manifest)
    (root / "manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")

    by_day = {}
    for row in daily_rows:
        by_day.setdefault(row["service_date"], []).append(row)
    if by_day:
        daily_dir = root / "daily"
        daily_dir.mkdir(exist_ok=True)
        for day, rows in by_day.items():
            _write_csv(daily_dir / f"{day}.csv", DAILY_COLUMNS, rows)

    by_up = {}
    for row in uptime_rows:
        by_up.setdefault(row["service_date"], []).append(row)
    if by_up:
        uptime_dir = root / "uptime"
        uptime_dir.mkdir(exist_ok=True)
        for day, rows in by_up.items():
            _write_csv(uptime_dir / f"{day}.csv", UPTIME_COLUMNS, rows)
    return root
