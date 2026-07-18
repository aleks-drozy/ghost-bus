"""Runtime configuration shared by the poller and classifier entry points.

Both processes read from and write to the same SQLite file - get_db() turns on
WAL mode plus a generous busy_timeout so one process's write never errors out
the other's read/write with "database is locked".
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = "state/ghostbus.db"
DEFAULT_ARCHIVE_DIR = "state/archive"
# These must match the agency_name column of the *static* GTFS agency.txt
# (the GTFS-Realtime feed itself carries no agency information at all) - NOT
# necessarily the operator's everyday brand name. VERIFY THIS AT DEPLOY TIME:
# run `python -m timetable.refresh` once against the live TFI zip and check
# its returned "agencies" list; open-data operator names are notoriously
# inconsistent ("Go-Ahead Ireland" vs "GoAhead Ireland" vs "Go Ahead Ireland").
# If the real names differ from the default below, override with the
# GHOSTBUS_AGENCIES env var - a silent mismatch here means every trip gets
# filtered out and the classifier reports nothing, with no error.
DEFAULT_AGENCIES = "Dublin Bus,Go-Ahead Ireland"


def get_db(path: str | None = None) -> sqlite3.Connection:
    db_path = Path(path if path is not None else os.environ.get("GHOSTBUS_DB", DEFAULT_DB_PATH))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def read_nta_api_key() -> str | None:
    return os.environ.get("NTA_API_KEY")


def read_archive_dir() -> Path:
    return Path(os.environ.get("GHOSTBUS_ARCHIVE", DEFAULT_ARCHIVE_DIR))


def read_agency_names() -> set[str]:
    raw = os.environ.get("GHOSTBUS_AGENCIES", DEFAULT_AGENCIES)
    return {name.strip() for name in raw.split(",") if name.strip()}
