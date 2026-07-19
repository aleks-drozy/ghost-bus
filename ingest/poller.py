"""GTFS-Realtime poller. fetch_fn/now_fn injected so tests never touch the network.

Production wiring (VM): fetch_fn wraps requests.get on the NTA endpoints with the
API key header; run_loop alternates TripUpdates and VehiclePositions at 60 s.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import time
from pathlib import Path
from typing import Callable

import zstandard
from google.transit import gtfs_realtime_pb2 as rt

from classify.store import record_heartbeat, record_observation

# Substrings SQLite actually uses for lock-contention errors (a writer
# holding the lock past busy_timeout, e.g. timetable.refresh's multi-minute
# DELETE+reinsert transaction - see ops/RUNBOOK.md 5.3). Deliberately narrow:
# any other OperationalError (a bad migration, a schema mismatch) must still
# raise rather than be mistaken for contention.
_LOCK_MESSAGES = ("database is locked", "database table is locked")


def _is_lock_contention(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc)
    return any(m in msg for m in _LOCK_MESSAGES)


def _service_date(start_date: str) -> str:
    return f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}" if len(start_date) == 8 else start_date


def _vehicle_ts(timestamp: int) -> str | None:
    """The vehicle's own report time as ISO-8601 UTC, or None if unusable.

    timestamp is uint64 POSIX seconds; 0 (the proto default) means the vehicle
    sent no report time - a genuine 1970 report is impossible. Values outside
    datetime's range are corrupt, and this field is measurement-only: degrade it
    to None rather than let it raise, which poll_once would otherwise catch as a
    failed poll and drop the whole batch of real positions with it.
    """
    if not timestamp:
        return None
    try:
        return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def parse_feed(raw: bytes) -> list[dict]:
    feed = rt.FeedMessage()
    feed.ParseFromString(raw)
    if not feed.header.gtfs_realtime_version:
        # ParseFromString does not raise on arbitrary bytes that happen to be valid
        # (empty) protobuf - a missing version is our signal this was never really
        # a GTFS-Realtime feed (e.g. an HTML error page from a gateway).
        raise ValueError("invalid feed")
    out: list[dict] = []
    for entity in feed.entity:
        if entity.HasField("trip_update"):
            tu = entity.trip_update
            if tu.trip.schedule_relationship == rt.TripDescriptor.CANCELED:
                out.append({"trip_id": tu.trip.trip_id, "kind": "cancel",
                            "stop_sequence": None, "start_date": tu.trip.start_date,
                            "lat": None, "lon": None, "vehicle_ts": None})
            else:
                seqs = [stu.stop_sequence for stu in tu.stop_time_update
                        if stu.HasField("stop_sequence")]
                out.append({"trip_id": tu.trip.trip_id, "kind": "update",
                            "stop_sequence": max(seqs) if seqs else None,
                            "start_date": tu.trip.start_date,
                            "lat": None, "lon": None, "vehicle_ts": None})
        elif entity.HasField("vehicle"):
            v = entity.vehicle
            has_pos = v.HasField("position")
            out.append({"trip_id": v.trip.trip_id, "kind": "position",
                        "stop_sequence": v.current_stop_sequence if v.HasField("current_stop_sequence") else None,
                        "start_date": v.trip.start_date,
                        "lat": v.position.latitude if has_pos else None,
                        "lon": v.position.longitude if has_pos else None,
                        "vehicle_ts": _vehicle_ts(v.timestamp)})
    return out


def poll_once(db: sqlite3.Connection, fetch_fn: Callable[[], bytes],
              now_fn: Callable[[], dt.datetime], route_filter: set[str] | None,
              archive_dir: Path | None) -> int:
    """Fetch and ingest one batch of GTFS-Realtime observations.

    route_filter is reserved; observations are not filtered at ingest - route scoping
    happens at classify time via the timetable join. A poll only counts as
    successful (ok=True heartbeat, archived snapshot) once the fetch AND the parse
    both succeed - an unparseable response (e.g. a gateway error page) is a failed
    poll, not a zero-observation success. Observations whose start_date doesn't
    parse to 8 digits are dropped: an unmatchable service key is never mis-keyed
    as "" and silently orphaned from every scheduled trip instead.

    A locked database (e.g. timetable.refresh holding SQLite's write lock for
    the several minutes a full national stop_times reload takes - RUNBOOK 5.3)
    is a skipped poll, not a crash: sqlite3.OperationalError from any of the
    three store writes below is caught and turned into the -1 failed-poll
    sentinel when it names lock contention, instead of propagating and killing
    the process (systemd's Restart=always would only re-exec straight back
    into the same lock, turning one skipped poll into a crash-loop that loses
    many). No heartbeat is written for a poll that fails this way, so the gap
    counts honestly as tracker downtime. A lock hit partway through the
    observation loop is treated the same - the poll is incomplete, so it
    returns -1 rather than the partial count, even though any observations
    already committed before the lock hit (record_observation commits per
    call) legitimately stay in the database. Any OperationalError that is NOT
    lock contention (e.g. "no such table" from a bad migration) is re-raised
    unchanged - that class must fail loudly, not be absorbed here.
    """
    now = now_fn()
    try:
        raw = fetch_fn()
        parsed = parse_feed(raw)
    except Exception:
        try:
            record_heartbeat(db, now.isoformat(), False)
        except sqlite3.OperationalError as exc:
            if not _is_lock_contention(exc):
                raise
            print(f"poll_once: database locked while recording failed-poll "
                  f"heartbeat ({exc}) - skipping this poll, gap counts as "
                  f"tracker downtime", file=sys.stderr)
        return -1
    try:
        record_heartbeat(db, now.isoformat(), True)
    except sqlite3.OperationalError as exc:
        if not _is_lock_contention(exc):
            raise
        print(f"poll_once: database locked while recording heartbeat ({exc}) "
              f"- skipping this poll, gap counts as tracker downtime",
              file=sys.stderr)
        return -1
    if archive_dir is not None:
        day_dir = Path(archive_dir) / now.strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / f"{now.strftime('%H%M%S')}.pb.zst").write_bytes(
            zstandard.ZstdCompressor().compress(raw))
    count = 0
    for obs in parsed:
        if not obs["trip_id"]:
            continue
        if len(obs["start_date"]) != 8 or not obs["start_date"].isdigit():
            continue
        try:
            record_observation(db, obs["trip_id"], _service_date(obs["start_date"]),
                               now.isoformat(), obs["kind"], obs["stop_sequence"],
                               obs["lat"], obs["lon"], obs["vehicle_ts"])
        except sqlite3.OperationalError as exc:
            if not _is_lock_contention(exc):
                raise
            print(f"poll_once: database locked while recording an observation "
                  f"({exc}) - poll incomplete after {count} observation(s) "
                  f"already written, skipping the rest of this poll",
                  file=sys.stderr)
            return -1
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
