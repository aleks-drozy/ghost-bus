from tests.site_fixtures import daily_row, uptime_row, write_dataset

from publish.site import read_daily, read_manifest, read_uptime


def test_read_manifest_returns_the_published_json(tmp_path):
    data = write_dataset(tmp_path / "data")
    manifest = read_manifest(data)
    assert manifest["schema_version"] == 1
    assert manifest["coverage"]["complete_days"] == 28


def test_read_daily_coerces_counts_to_int_and_rates_to_float(tmp_path):
    data = write_dataset(
        tmp_path / "data",
        daily_rows=[
            daily_row(
                "2026-06-01", "03C 120 e a",
                route_short_name="120", route_long_name="Main Street",
                agency_name="Dublin Bus",
                scheduled=10, excluded=1, cancelled=0, completed=7, vanished=1, untracked=1,
                vanished_rate=0.1111, vanished_lo=0.0198, vanished_hi=0.4348,
                untracked_rate=0.1111, untracked_lo=0.0198, untracked_hi=0.4348,
            )
        ],
    )
    rows = read_daily(data)
    assert len(rows) == 1
    row = rows[0]
    assert row["route_id"] == "03C 120 e a"
    assert row["scheduled"] == 10 and isinstance(row["scheduled"], int)
    assert row["vanished_rate"] == 0.1111 and isinstance(row["vanished_rate"], float)


def test_read_daily_reads_every_day_file_sorted_by_date(tmp_path):
    data = write_dataset(
        tmp_path / "data",
        daily_rows=[
            daily_row("2026-06-02", "R1", scheduled=2),
            daily_row("2026-06-01", "R1", scheduled=1),
        ],
    )
    assert [r["service_date"] for r in read_daily(data)] == ["2026-06-01", "2026-06-02"]


def test_read_daily_maps_blank_rate_to_none_never_zero(tmp_path):
    data = write_dataset(
        tmp_path / "data",
        daily_rows=[daily_row("2026-06-01", "R1", scheduled=3, excluded=3)],
    )
    row = read_daily(data)[0]
    assert row["vanished_rate"] is None
    assert row["untracked_hi"] is None


def test_read_daily_on_a_dataset_with_no_daily_dir_is_empty(tmp_path):
    data = write_dataset(tmp_path / "data", manifest={"scoreboard_ready": False})
    assert read_daily(data) == []


def test_read_uptime_parses_fractions(tmp_path):
    data = write_dataset(
        tmp_path / "data",
        uptime_rows=[uptime_row("2026-06-01", 1440, 1436)],
    )
    rows = read_uptime(data)
    assert rows[0]["ok_minutes"] == 1436
    assert 0.997 < rows[0]["uptime_fraction"] < 0.998


def test_read_daily_round_trips_through_the_fixture_writer(tmp_path):
    """Later tasks rewrite a dataset by feeding read_daily's output back in."""
    data = write_dataset(tmp_path / "data",
                         daily_rows=[daily_row("2026-06-01", "R1", scheduled=4,
                                               excluded=1, vanished=1, untracked=0,
                                               cancelled=0, completed=2)])
    first = read_daily(data)
    write_dataset(tmp_path / "data", daily_rows=first)
    assert read_daily(data) == first
