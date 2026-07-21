import datetime as dt
import sqlite3

from publish.dataset import (DAILY_COLUMNS, complete_service_days, daily_rows,
                             route_names, unnamed_routes, write_daily_csvs)
from tests.dataset_fixture import SERVICE_DATE, build_db, consecutive_dates

# Hand-checked Wilson bounds at z=1.96:
#   1/8 -> 0.125000 [0.022417, 0.470895]
#   1/2 -> 0.500000 [0.094529, 0.905471]
#   0/2 -> 0.000000 [0.000000, 0.657628]
GOLDEN_DAILY = (
    "service_date,route_id,route_short_name,route_long_name,agency_name,"
    "scheduled,excluded,cancelled,completed,vanished,untracked,"
    "vanished_rate,vanished_lo,vanished_hi,"
    "untracked_rate,untracked_lo,untracked_hi\n"
    "2026-03-23,03C 120 e a,,,,2,0,0,1,1,0,"
    "0.500000,0.094529,0.905471,0.000000,0.000000,0.657628\n"
    "2026-03-23,R1,1,Fixtureville Main,Fixtureville Bus,10,2,1,5,1,1,"
    "0.125000,0.022417,0.470895,0.125000,0.022417,0.470895\n"
    "2026-03-23,R2,2,Fixtureville Orbital,Fixtureville Bus,1,1,0,0,0,0,"
    ",,,,,\n"
)


def test_daily_columns_match_the_spec_verbatim():
    assert DAILY_COLUMNS == (
        "service_date", "route_id", "route_short_name", "route_long_name",
        "agency_name", "scheduled", "excluded", "cancelled", "completed",
        "vanished", "untracked",
        "vanished_rate", "vanished_lo", "vanished_hi",
        "untracked_rate", "untracked_lo", "untracked_hi")


def test_no_column_sums_the_two_rates():
    # Spec D1: the two rates are never summed by any code path, and no
    # combined field is published under any name.
    for banned in ("ghost_rate", "combined_rate", "unreliable_rate",
                   "vanished_plus_untracked", "failure_rate"):
        assert banned not in DAILY_COLUMNS


def test_golden_daily_csv(tmp_path):
    db = build_db()
    names = route_names(db)
    written = write_daily_csvs(db, tmp_path, [SERVICE_DATE], names)
    assert written == [tmp_path / "daily" / "2026-03-23.csv"]
    assert written[0].read_bytes() == GOLDEN_DAILY.encode("utf-8")


def test_rows_are_sorted_by_route_id():
    db = build_db()
    rows = daily_rows(db, SERVICE_DATE, route_names(db))
    assert [r["route_id"] for r in rows] == ["03C 120 e a", "R1", "R2"]


def test_zero_denominator_publishes_empty_cells_never_zero():
    db = build_db()
    r2 = next(r for r in daily_rows(db, SERVICE_DATE, route_names(db))
              if r["route_id"] == "R2")
    for column in ("vanished_rate", "vanished_lo", "vanished_hi",
                   "untracked_rate", "untracked_lo", "untracked_hi"):
        assert r2[column] == "", column


def test_route_missing_from_gtfs_falls_back_to_raw_id_and_is_listed():
    db = build_db()
    names = route_names(db)
    unnamed = next(r for r in daily_rows(db, SERVICE_DATE, names)
                   if r["route_id"] == "03C 120 e a")
    assert unnamed["route_short_name"] == ""
    assert unnamed["route_long_name"] == ""
    assert unnamed["agency_name"] == ""
    assert unnamed_routes(db, names) == ["03C 120 e a"]


def test_todays_partial_day_is_excluded():
    db = build_db(service_dates=("2026-03-23", "2026-03-24"))
    assert complete_service_days(db, dt.date(2026, 3, 24)) == ["2026-03-23"]
    assert complete_service_days(db, dt.date(2026, 3, 25)) == ["2026-03-23",
                                                               "2026-03-24"]


def test_route_names_survive_a_database_with_no_gtfs_tables():
    db = sqlite3.connect(":memory:")
    db.executescript("""
    CREATE TABLE trip_outcomes (
      trip_id TEXT, service_date TEXT, route_id TEXT, start_utc TEXT, outcome TEXT,
      PRIMARY KEY (trip_id, service_date));
    """)
    db.execute("INSERT INTO trip_outcomes VALUES ('t','2026-03-23','R9',"
               "'2026-03-23T07:00:00+00:00','COMPLETED')")
    db.commit()
    assert route_names(db) == {}
    # Every route then surfaces as unnamed rather than being silently dropped.
    assert unnamed_routes(db, {}) == ["R9"]


def test_write_daily_csvs_prunes_a_csv_whose_day_dropped_out_of_coverage(tmp_path):
    """C1: reproduces the orphan bug directly at the write_daily_csvs level.

    Publish 20 days, then republish with only the first 15 still in `days`
    (as happens when rows for the later days are deleted from
    trip_outcomes, e.g. RUNBOOK 8.4's recovery from a failed gate). The five
    orphaned CSVs must be removed, not left on disk to be read back by
    publish/site.py's directory scan and enter a window the manifest no
    longer claims.
    """
    db = build_db(service_dates=consecutive_dates(20))
    names = route_names(db)
    all_days = consecutive_dates(20)
    write_daily_csvs(db, tmp_path, all_days, names)
    assert sorted(p.name for p in (tmp_path / "daily").iterdir()) == \
        [f"{d}.csv" for d in all_days]

    kept_days = all_days[:15]
    write_daily_csvs(db, tmp_path, kept_days, names)
    assert sorted(p.name for p in (tmp_path / "daily").iterdir()) == \
        [f"{d}.csv" for d in kept_days]


def test_write_daily_csvs_rolls_the_outcomes_table_up_once(tmp_path, monkeypatch):
    """One full-table rollup per run, not one per published day.

    route_day_rollup SELECTs the whole trip_outcomes table with no WHERE. A
    year of Dublin-scale data called once per day would be ~3M rows re-rolled
    365 times on a 1 GB VM.
    """
    import publish.dataset as dataset

    real = dataset.route_day_rollup
    calls = []

    def counting(db):
        calls.append(1)
        return real(db)

    monkeypatch.setattr(dataset, "route_day_rollup", counting)
    days = consecutive_dates(14)
    db = build_db(service_dates=days)
    dataset.write_daily_csvs(db, tmp_path, days, {})
    assert len(calls) == 1
    assert len(list((tmp_path / "daily").iterdir())) == 14
