"""GTFS static timetable refresh entry point.

refresh() is the testable core: fetch_fn is injected so tests never touch the
network. main() (pragma no cover) is the Phase-2 production entry point - it
downloads the real TFI operator GTFS zip and loads it into the shared runtime
db. Intended to run on a weekly schedule (see ops/RUNBOOK.md), and on demand
when trip-match failures spike.

Runnable as: python -m timetable.refresh
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Callable

from timetable.gtfs import load_gtfs

# Confirmed against the live TFI feed 2026-07-18: this is the static GTFS zip
# despite the "Realtime" in the filename - not the GTFS-R protobuf endpoint.
GTFS_STATIC_ZIP_URL = "https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip"


def refresh(db: sqlite3.Connection, fetch_fn: Callable[[], bytes],
            dest_zip_path: str | Path) -> dict:
    """Fetch the operator GTFS zip, persist it, load it, and summarize the result."""
    raw = fetch_fn()
    dest = Path(dest_zip_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    gtfs_hash = load_gtfs(dest, db)
    (n_trips,) = db.execute("SELECT COUNT(*) FROM gtfs_trips").fetchone()
    agencies = [name for (name,) in db.execute("SELECT agency_name FROM gtfs_agency")]
    return {"gtfs_hash": gtfs_hash, "n_trips": n_trips, "agencies": agencies}


def main() -> int:  # pragma: no cover
    import requests

    from ghostbus_config import get_db

    db_path = Path(os.environ.get("GHOSTBUS_DB", "state/ghostbus.db"))
    db = get_db()

    def fetch() -> bytes:
        resp = requests.get(GTFS_STATIC_ZIP_URL, timeout=120)
        resp.raise_for_status()
        return resp.content

    dest_zip_path = db_path.parent / "gtfs_static.zip"
    summary = refresh(db, fetch, dest_zip_path)
    print(summary)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
