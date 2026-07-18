"""GTFS-Realtime poller. fetch_fn/now_fn injected so tests never touch the network.

Production wiring (VM): fetch_fn wraps requests.get on the NTA endpoints with the
API key header; run_loop alternates TripUpdates and VehiclePositions at 60 s.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import time
from pathlib import Path
from typing import Callable

import zstandard
from google.transit import gtfs_realtime_pb2 as rt

from classify.store import record_heartbeat, record_observation


def _service_date(start_date: str) -> str:
    return f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}" if len(start_date) == 8 else start_date


def parse_feed(raw: bytes) -> list[dict]:
    feed = rt.FeedMessage()
    feed.ParseFromString(raw)
    out: list[dict] = []
    for entity in feed.entity:
        if entity.HasField("trip_update"):
            tu = entity.trip_update
            if tu.trip.schedule_relationship == rt.TripDescriptor.CANCELED:
                out.append({"trip_id": tu.trip.trip_id, "kind": "cancel",
                            "stop_sequence": None, "start_date": tu.trip.start_date})
            else:
                seqs = [stu.stop_sequence for stu in tu.stop_time_update
                        if stu.HasField("stop_sequence")]
                out.append({"trip_id": tu.trip.trip_id, "kind": "update",
                            "stop_sequence": max(seqs) if seqs else None,
                            "start_date": tu.trip.start_date})
        elif entity.HasField("vehicle"):
            v = entity.vehicle
            out.append({"trip_id": v.trip.trip_id, "kind": "position",
                        "stop_sequence": v.current_stop_sequence or None,
                        "start_date": v.trip.start_date})
    return out


def poll_once(db: sqlite3.Connection, fetch_fn: Callable[[], bytes],
              now_fn: Callable[[], dt.datetime], route_filter: set[str] | None,
              archive_dir: Path | None) -> int:
    now = now_fn()
    try:
        raw = fetch_fn()
    except Exception:
        record_heartbeat(db, now.isoformat(), False)
        return -1
    record_heartbeat(db, now.isoformat(), True)
    if archive_dir is not None:
        day_dir = Path(archive_dir) / now.strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / f"{now.strftime('%H%M%S')}.pb.zst").write_bytes(
            zstandard.ZstdCompressor().compress(raw))
    count = 0
    for obs in parse_feed(raw):
        if not obs["trip_id"]:
            continue
        if route_filter is not None and obs.get("route_id") not in route_filter:
            pass  # route filtering happens at classify time via the timetable join
        record_observation(db, obs["trip_id"], _service_date(obs["start_date"]),
                           now.isoformat(), obs["kind"], obs["stop_sequence"])
        count += 1
    return count


def run_loop(db, fetch_fns: list[Callable[[], bytes]], archive_dir: Path,
             interval_s: int = 60) -> None:  # pragma: no cover
    i = 0
    while True:
        poll_once(db, fetch_fns[i % len(fetch_fns)],
                  lambda: dt.datetime.now(dt.timezone.utc), None, archive_dir)
        i += 1
        time.sleep(interval_s)
