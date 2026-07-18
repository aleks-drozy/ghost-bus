"""Poller production entry point.

Alternates the two NTA GTFS-Realtime endpoints (TripUpdates, VehiclePositions)
on a 60 s cadence via ingest.poller.run_loop, so each individual feed is
sampled every 120 s - the conservative default from the design spec pending
clarification of whether the NTA's "1 call per 60s per token" fair-usage rule
is per-token or per-endpoint.

Runnable as: python -m ingest.run_poller
"""
from __future__ import annotations

import sys

from classify.store import init_store
from ghostbus_config import get_db, read_archive_dir, read_nta_api_key
from ingest.poller import run_loop

# Confirmed against the live NTA feed 2026-07-18.
TRIP_UPDATES_URL = "https://api.nationaltransport.ie/gtfsr/v2/gtfsr"
VEHICLE_POSITIONS_URL = "https://api.nationaltransport.ie/gtfsr/v2/Vehicles"


def _make_fetch(url: str, api_key: str):
    import requests

    def fetch() -> bytes:
        resp = requests.get(url, headers={"x-api-key": api_key}, timeout=30)
        resp.raise_for_status()
        return resp.content

    return fetch


def main() -> int:  # pragma: no cover
    api_key = read_nta_api_key()
    if not api_key:
        print("NTA_API_KEY is not set - register a free key at "
              "developer.nationaltransport.ie and export it before running the "
              "poller (see ops/RUNBOOK.md 1.1).", file=sys.stderr)
        return 2

    db = get_db()
    init_store(db)
    archive_dir = read_archive_dir()
    fetch_fns = [_make_fetch(TRIP_UPDATES_URL, api_key),
                 _make_fetch(VEHICLE_POSITIONS_URL, api_key)]
    run_loop(db, fetch_fns, archive_dir, interval_s=60)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
