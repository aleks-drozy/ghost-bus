import datetime as dt
import json

from publish.dataset import (BASELINE_REQUIRED_DAYS, build_manifest,
                             published_slugs, route_names, write_dataset)
from tests.dataset_fixture import (GTFS_HASH, GTFS_LOADED_AT, SERVICE_DATE,
                                   build_db, consecutive_dates)

UTC = dt.timezone.utc
FIXED_NOW = dt.datetime(2026, 3, 24, 4, 15, 0, tzinfo=UTC)


def read_manifest(data_dir):
    return json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))


def test_manifest_has_exactly_the_spec_keys(tmp_path):
    db = build_db()
    write_dataset(db, tmp_path, today=dt.date(2026, 3, 24), now_utc=FIXED_NOW)
    manifest = read_manifest(tmp_path)
    assert list(manifest) == ["schema_version", "generated_at", "timetable_hash",
                              "timetable_loaded_at", "coverage", "scoreboard_ready",
                              "baseline_required_days", "gate", "counts",
                              "unnamed_routes", "route_slugs"]
    assert list(manifest["coverage"]) == ["first_day", "last_day", "complete_days"]
    assert list(manifest["gate"]) == ["conservation", "rates_bounded",
                                      "outcomes_valid"]
    assert list(manifest["counts"]) == ["observations", "snapshots",
                                        "trips_classified"]


def test_manifest_values_for_a_single_complete_day(tmp_path):
    db = build_db()
    write_dataset(db, tmp_path, today=dt.date(2026, 3, 24), now_utc=FIXED_NOW)
    assert read_manifest(tmp_path) == {
        "schema_version": 1,
        "generated_at": "2026-03-24T04:15:00+00:00",
        "timetable_hash": GTFS_HASH,
        "timetable_loaded_at": GTFS_LOADED_AT,
        "coverage": {"first_day": SERVICE_DATE, "last_day": SERVICE_DATE,
                     "complete_days": 1},
        "scoreboard_ready": False,
        "baseline_required_days": 14,
        "gate": {"conservation": True, "rates_bounded": True,
                 "outcomes_valid": True},
        "counts": {"observations": 3, "snapshots": 3, "trips_classified": 13},
        "unnamed_routes": ["03C 120 e a"],
        "route_slugs": {"03C 120 e a": "03c-120-e-a", "R1": "r1", "R2": "r2"},
    }


def test_manifest_file_is_pretty_printed_and_newline_terminated(tmp_path):
    db = build_db()
    write_dataset(db, tmp_path, today=dt.date(2026, 3, 24), now_utc=FIXED_NOW)
    text = (tmp_path / "manifest.json").read_text(encoding="utf-8")
    assert text.startswith('{\n  "schema_version": 1,\n')
    assert text.endswith("\n")
    assert "\r" not in text


def test_thirteen_complete_days_publish_no_route_csvs(tmp_path):
    days = consecutive_dates(13)          # 2026-03-02 .. 2026-03-14
    db = build_db(service_dates=days)
    write_dataset(db, tmp_path, today=dt.date(2026, 3, 15), now_utc=FIXED_NOW)
    manifest = read_manifest(tmp_path)
    assert manifest["coverage"]["complete_days"] == 13
    assert manifest["scoreboard_ready"] is False
    assert not (tmp_path / "daily").exists()


def test_fourteen_complete_days_flip_the_scoreboard_on(tmp_path):
    days = consecutive_dates(14)          # 2026-03-02 .. 2026-03-15
    db = build_db(service_dates=days)
    write_dataset(db, tmp_path, today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    manifest = read_manifest(tmp_path)
    assert manifest["coverage"] == {"first_day": "2026-03-02",
                                    "last_day": "2026-03-15",
                                    "complete_days": 14}
    assert manifest["scoreboard_ready"] is True
    assert BASELINE_REQUIRED_DAYS == 14
    written = sorted(p.name for p in (tmp_path / "daily").iterdir())
    assert written == [f"{d}.csv" for d in days]


def test_falling_below_the_baseline_withdraws_published_route_csvs(tmp_path):
    """The gate is a state, not an event.

    data/ is a working copy of what is already public. If coverage falls back
    below the threshold, route data must be withdrawn, not left standing beside
    a page that says we publish nothing about any route.
    """
    write_dataset(build_db(service_dates=consecutive_dates(14)), tmp_path,
                  today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    assert list((tmp_path / "daily").iterdir())
    write_dataset(build_db(service_dates=consecutive_dates(13)), tmp_path,
                  today=dt.date(2026, 3, 15), now_utc=FIXED_NOW)
    assert not (tmp_path / "daily").exists()
    assert read_manifest(tmp_path)["scoreboard_ready"] is False


def test_uptime_is_exempt_from_the_baseline_gate(tmp_path):
    # Day one: no route data may ship, but our own downtime always does.
    db = build_db()
    write_dataset(db, tmp_path, today=dt.date(2026, 3, 24), now_utc=FIXED_NOW)
    assert not (tmp_path / "daily").exists()
    assert (tmp_path / "uptime" / "2026-03-23.csv").exists()


def test_empty_database_yields_null_coverage(tmp_path):
    db = build_db(service_dates=(), heartbeats=[])
    write_dataset(db, tmp_path, today=dt.date(2026, 3, 24), now_utc=FIXED_NOW)
    manifest = read_manifest(tmp_path)
    assert manifest["coverage"] == {"first_day": None, "last_day": None,
                                    "complete_days": 0}
    assert manifest["scoreboard_ready"] is False


def test_build_manifest_is_pure_and_writes_nothing(tmp_path):
    db = build_db()
    names = route_names(db)
    manifest = build_manifest(db, [SERVICE_DATE],
                              {"conservation": True, "rates_bounded": True,
                               "outcomes_valid": True}, names,
                              {"R1": "r1"}, FIXED_NOW)
    assert manifest["generated_at"] == "2026-03-24T04:15:00+00:00"
    assert manifest["route_slugs"] == {"R1": "r1"}
    assert list(tmp_path.iterdir()) == []


def test_a_new_route_cannot_take_a_slug_published_to_an_incumbent():
    """Assignment must honour what is already public.

    "03C 120 e a" sorts before "03C/120/e/a" (0x20 < 0x2F), so on a fresh
    assignment the newcomer would take the bare slug the incumbent is already
    live under, and a published route URL would move.
    """
    got = published_slugs(["03C 120 e a", "03C/120/e/a"],
                          {"03C/120/e/a": "03c-120-e-a"})
    assert got["03C/120/e/a"] == "03c-120-e-a"
    assert got["03C 120 e a"] == "03c-120-e-a-2"


def test_a_retired_routes_slug_is_carried_forward_and_never_reassigned():
    """A withdrawn route's URL must keep resolving to that route.

    "GONE" has dropped out of the current window, so slug_map on its own would
    drop it from the map and hand "gone" to the next route that slugifies the
    same way - silently pointing an existing public link at a different route.
    """
    got = published_slugs(["gone", "R1"], {"GONE": "gone", "R1": "r1"})
    assert got["GONE"] == "gone"
    assert got["gone"] == "gone-2"
    assert got["R1"] == "r1"


def test_the_published_slug_map_is_stable_across_two_publishes(tmp_path):
    """Second publish reads the first one's manifest back off disk."""
    expected = {"03C 120 e a": "03c-120-e-a", "R1": "r1", "R2": "r2"}
    first = write_dataset(build_db(service_dates=consecutive_dates(14)), tmp_path,
                          today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    second = write_dataset(build_db(service_dates=consecutive_dates(14)), tmp_path,
                           today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    assert first["route_slugs"] == expected
    assert second["route_slugs"] == expected
    assert read_manifest(tmp_path)["route_slugs"] == expected
