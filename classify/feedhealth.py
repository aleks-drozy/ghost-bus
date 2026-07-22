"""Amendment G3: per-operator feed-health detection, schedule-relative.

The 2026-07-21 incident: the NTA VehiclePositions feed partially collapsed
for ~40 minutes (whole vehicles disappearing, not pings-per-vehicle), and
trips genuinely in motion matched the VANISHED rule. Our uptime gate saw
nothing - the poller was healthy; the *feed* was not. The classifier
converts trips caught by this detector out of the accusatory classes into
EXCLUDED_FEED (see classify/outcomes.py).

Signal: for each graded operator and 10-minute bucket,
    reporting_fraction = scheduled trips active in the bucket that produced
                         >= 1 position ping in it / scheduled trips active.
The denominator comes from the operator's own timetable, so the baseline is
day-class aware by construction (Saturday dawn is compared with Saturday's
schedule, not a weekday's), needs no trailing-window warm-up, and cannot be
contaminated by a multi-day outage. Healthy daytime fractions measured
~0.85-1.0; the 2026-07-21 trough was ~0.25.

The constants below are methodology, not tuning knobs: changing any of them
changes what the public numbers mean, so they are deliberately NOT
environment variables - a change must be a commit, in public, like the
amendment itself.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

from timetable.gtfs import ScheduledTrip

BUCKET_S = 600
# Wide margin from both sides: healthy >= ~0.85, the real incident ~0.25.
THRESHOLD = 0.5
# Below this many active trips a fraction is noise, not evidence (overnight).
MIN_ACTIVE_TRIPS = 30
# One noisy bucket must not blank an interval.
MIN_RUN = 2
# Buckets where OUR uptime is below this are not evaluated at all: our
# downtime must never read as feed degradation (EXCLUDED owns those trips).
UPTIME_GUARD = 0.9


def bucket_index(ts: dt.datetime) -> int:
    return int(ts.timestamp()) // BUCKET_S


def _active_buckets(trip: ScheduledTrip) -> range:
    """Buckets overlapping the scheduled span [start, end) - half-open, so a
    trip ending exactly on a bucket boundary is not 'active' in a bucket it
    never ran in (which would fabricate a zero-reporting bucket)."""
    first = bucket_index(trip.start_utc)
    last = bucket_index(trip.end_utc - dt.timedelta(microseconds=1))
    return range(first, last + 1)


def find_degraded_runs(active: dict[int, int], reporting: dict[int, int],
                       unwatched: set[int]) -> list[tuple[int, int]]:
    """Half-open [start_bucket, end_bucket) runs where the feed was degraded.

    A bucket is degraded when active >= MIN_ACTIVE_TRIPS, the tracker was
    watching, and reporting/active < THRESHOLD. Only runs of >= MIN_RUN
    strictly consecutive degraded buckets arm the gate; an unwatched or
    below-minimum bucket breaks a run rather than bridging two half-runs.
    """
    degraded = sorted(
        b for b, n in active.items()
        if n >= MIN_ACTIVE_TRIPS and b not in unwatched
        and reporting.get(b, 0) / n < THRESHOLD)
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(degraded):
        j = i
        while j + 1 < len(degraded) and degraded[j + 1] == degraded[j] + 1:
            j += 1
        if j - i + 1 >= MIN_RUN:
            runs.append((degraded[i], degraded[j] + 1))
        i = j + 1
    return runs


def _unwatched_buckets(db: sqlite3.Connection, first: int, last: int) -> set[int]:
    """Buckets in [first, last] whose heartbeat minute-coverage < UPTIME_GUARD."""
    start = dt.datetime.fromtimestamp(first * BUCKET_S, tz=dt.timezone.utc)
    end = dt.datetime.fromtimestamp((last + 1) * BUCKET_S, tz=dt.timezone.utc)
    minutes = {m for (m,) in db.execute(
        "SELECT DISTINCT substr(ts_utc,1,16) FROM heartbeats "
        "WHERE ok=1 AND ts_utc>=? AND ts_utc<?",
        (start.isoformat(), end.isoformat()))}
    ok_per_bucket: dict[int, int] = {}
    for stamp in minutes:
        ts = dt.datetime.fromisoformat(stamp + ":00+00:00")
        b = bucket_index(ts)
        ok_per_bucket[b] = ok_per_bucket.get(b, 0) + 1
    per_bucket_minutes = BUCKET_S / 60.0
    return {b for b in range(first, last + 1)
            if ok_per_bucket.get(b, 0) / per_bucket_minutes < UPTIME_GUARD}


def route_agency_map(db: sqlite3.Connection) -> dict[str, str]:
    """route_id -> agency_name. A database predating the timetable load has
    no agency tables: the map is empty, no shields ever fire, and the
    classifier behaves exactly as pre-G3. Anything else must crash - quietly
    losing the map would silently re-enable accusations during feed outages.
    """
    try:
        rows = db.execute(
            "SELECT r.route_id, a.agency_name FROM gtfs_routes r "
            "JOIN gtfs_agency a ON a.agency_id = r.agency_id").fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {}
        raise
    return {route_id: name for route_id, name in rows if name}


def compute_shields(db: sqlite3.Connection, trips: list[ScheduledTrip],
                    agency_of_route: dict[str, str],
                    ) -> dict[str, list[tuple[dt.datetime, dt.datetime]]]:
    """Degraded intervals per agency, as UTC (start, end) pairs.

    One indexed observations query per trip (idx_obs_trip) - the same access
    pattern as classification itself; no full-table scan, no new index.
    """
    active: dict[str, dict[int, int]] = {}
    reporting: dict[str, dict[int, int]] = {}
    for trip in trips:
        agency = agency_of_route.get(trip.route_id)
        if agency is None:
            continue
        buckets = _active_buckets(trip)
        a = active.setdefault(agency, {})
        for b in buckets:
            a[b] = a.get(b, 0) + 1
        rows = db.execute(
            "SELECT ts_utc FROM observations WHERE trip_id=? AND service_date=? "
            "AND kind='position' AND ts_utc>=? AND ts_utc<?",
            (trip.trip_id, str(trip.service_date),
             trip.start_utc.isoformat(), trip.end_utc.isoformat())).fetchall()
        seen = {bucket_index(dt.datetime.fromisoformat(ts)) for (ts,) in rows}
        r = reporting.setdefault(agency, {})
        for b in seen.intersection(buckets):
            r[b] = r.get(b, 0) + 1
    if not active:
        return {}
    all_buckets = [b for per in active.values() for b in per]
    unwatched = _unwatched_buckets(db, min(all_buckets), max(all_buckets))
    shields: dict[str, list[tuple[dt.datetime, dt.datetime]]] = {}
    for agency, per in active.items():
        runs = find_degraded_runs(per, reporting.get(agency, {}), unwatched)
        if runs:
            shields[agency] = [
                (dt.datetime.fromtimestamp(s * BUCKET_S, tz=dt.timezone.utc),
                 dt.datetime.fromtimestamp(e * BUCKET_S, tz=dt.timezone.utc))
                for s, e in runs]
    return shields
