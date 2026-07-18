import csv
import io
import zipfile

from tests.fixtureville import build_gtfs_zip

REQUIRED = ["agency.txt", "stops.txt", "routes.txt", "trips.txt",
            "stop_times.txt", "calendar.txt"]


def read(zf: zipfile.ZipFile, name: str) -> list[dict]:
    with zf.open(name) as fh:
        return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8")))


def test_zip_contains_required_files(tmp_path):
    path = tmp_path / "fixtureville.zip"
    build_gtfs_zip(path)
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
    assert set(REQUIRED) <= names


def test_trip_and_service_shape(tmp_path):
    path = tmp_path / "fixtureville.zip"
    build_gtfs_zip(path)
    with zipfile.ZipFile(path) as zf:
        trips = read(zf, "trips.txt")
        stop_times = read(zf, "stop_times.txt")
        calendar = read(zf, "calendar.txt")
    trip_ids = {t["trip_id"] for t in trips}
    assert {"R1_late", "R1_wk_00", "R2_wk_00", "R2_sat_00"} <= trip_ids
    assert len([t for t in trips if t["route_id"] == "R1"]) == 11  # 10 + late
    late_times = [st for st in stop_times if st["trip_id"] == "R1_late"]
    assert late_times[0]["departure_time"] == "24:30:00"  # past-midnight trip
    services = {c["service_id"]: c for c in calendar}
    assert services["WK"]["monday"] == "1" and services["WK"]["saturday"] == "0"
    assert services["SAT"]["saturday"] == "1" and services["SAT"]["monday"] == "0"
    assert services["WK"]["start_date"] == "20260323"
    assert services["WK"]["end_date"] == "20260410"


def test_every_trip_has_ordered_stop_times(tmp_path):
    path = tmp_path / "fixtureville.zip"
    build_gtfs_zip(path)
    with zipfile.ZipFile(path) as zf:
        stop_times = read(zf, "stop_times.txt")
    by_trip: dict[str, list[int]] = {}
    for st in stop_times:
        by_trip.setdefault(st["trip_id"], []).append(int(st["stop_sequence"]))
    for trip_id, seqs in by_trip.items():
        assert seqs == sorted(seqs) and len(seqs) >= 4, trip_id
