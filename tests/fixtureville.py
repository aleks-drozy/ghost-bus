"""Synthetic GTFS network for tests: 2 routes, WK+SAT services, a past-midnight
trip, valid across the 2026-03-29 DST change. Deterministic, built in-memory."""
from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

FIXTURE_TZ = "Europe/Dublin"

_STOPS_R1 = ["S1", "S2", "S3", "S4", "S5"]
_STOPS_R2 = ["S2", "S4", "S6", "S7"]


def _hms(total_seconds: int) -> str:
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _trip_rows(trip_id: str, start_s: int, duration_s: int, stops: list[str]):
    n = len(stops)
    step = duration_s // (n - 1)
    for seq, stop in enumerate(stops, start=1):
        t = _hms(start_s + (seq - 1) * step)
        yield {"trip_id": trip_id, "arrival_time": t, "departure_time": t,
               "stop_id": stop, "stop_sequence": str(seq)}


def build_gtfs_zip(path: str | Path) -> None:
    agency = [{"agency_id": "FVB", "agency_name": "Fixtureville Bus",
               "agency_url": "https://example.invalid", "agency_timezone": FIXTURE_TZ}]
    stops = [{"stop_id": s, "stop_name": f"Stop {s}", "stop_lat": "53.3", "stop_lon": "-6.2"}
             for s in sorted(set(_STOPS_R1 + _STOPS_R2))]
    routes = [{"route_id": "R1", "agency_id": "FVB", "route_short_name": "1",
               "route_long_name": "Fixtureville Main", "route_type": "3"},
              {"route_id": "R2", "agency_id": "FVB", "route_short_name": "2",
               "route_long_name": "Fixtureville Orbital", "route_type": "3"}]
    calendar = [
        {"service_id": "WK", "monday": "1", "tuesday": "1", "wednesday": "1",
         "thursday": "1", "friday": "1", "saturday": "0", "sunday": "0",
         "start_date": "20260323", "end_date": "20260410"},
        {"service_id": "SAT", "monday": "0", "tuesday": "0", "wednesday": "0",
         "thursday": "0", "friday": "0", "saturday": "1", "sunday": "0",
         "start_date": "20260323", "end_date": "20260410"},
    ]
    trips, stop_times = [], []

    def add_trip(trip_id, route_id, service_id, start_s, duration_s, stop_list):
        trips.append({"trip_id": trip_id, "route_id": route_id, "service_id": service_id})
        stop_times.extend(_trip_rows(trip_id, start_s, duration_s, stop_list))

    for i in range(10):  # half-hourly from 07:00, 60-minute run
        add_trip(f"R1_wk_{i:02d}", "R1", "WK", 7 * 3600 + i * 1800, 3600, _STOPS_R1)
    add_trip("R1_late", "R1", "WK", 24 * 3600 + 1800, 3600, _STOPS_R1)  # 24:30:00
    for i in range(5):  # from 08:15, 45-minute run
        add_trip(f"R2_wk_{i:02d}", "R2", "WK", 8 * 3600 + 900 + i * 3600, 2700, _STOPS_R2)
    add_trip("R2_sat_00", "R2", "SAT", 9 * 3600, 2700, _STOPS_R2)

    tables = {"agency.txt": agency, "stops.txt": stops, "routes.txt": routes,
              "trips.txt": trips, "stop_times.txt": stop_times, "calendar.txt": calendar}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, rows in tables.items():
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
            zf.writestr(name, buf.getvalue())
