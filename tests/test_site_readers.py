import pytest

# Importing publish.dataset here is fine and does not violate D3: D3 constrains
# the import graph of production publish/site.py (it must read only the
# published CSVs, never the database module), not test fixtures. This test
# module needs it to guard against the fixture schema drifting from the
# publisher's actual schema -- see test_daily_columns_fixture_matches_the_published_schema.
import publish.dataset as dataset
from tests.site_fixtures import DAILY_COLUMNS, UPTIME_COLUMNS, daily_row, uptime_row, write_dataset

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


def test_read_uptime_maps_blank_fraction_to_none(tmp_path):
    data = write_dataset(
        tmp_path / "data",
        uptime_rows=[uptime_row("2026-06-01", 1440, 1440, uptime_fraction="")],
    )
    row = read_uptime(data)[0]
    assert row["uptime_fraction"] is None


def test_read_uptime_on_a_dataset_with_no_uptime_dir_is_empty(tmp_path):
    data = write_dataset(tmp_path / "data")
    assert read_uptime(data) == []


def test_daily_columns_fixture_matches_the_published_schema():
    """publish/dataset.py owns DAILY_COLUMNS; tests/site_fixtures.py hand-copies
    it so site tests don't need a database fixture. Nothing enforced that the
    two stay in sync -- if the publisher's schema ever changes, every site
    task from here to Task 17 would keep passing against a shape the
    publisher no longer writes. This pins the two together.
    """
    assert tuple(DAILY_COLUMNS) == dataset.DAILY_COLUMNS


def test_uptime_columns_fixture_matches_the_published_schema():
    assert tuple(UPTIME_COLUMNS) == dataset.UPTIME_COLUMNS


def test_read_daily_rejects_a_non_finite_rate(tmp_path):
    """1e999 parses to float('inf') with no exception from float() itself --
    the domain check is what has to catch it."""
    data = write_dataset(
        tmp_path / "data",
        daily_rows=[daily_row("2026-06-01", "R1", scheduled=3, vanished_rate="1e999")],
    )
    with pytest.raises(ValueError) as exc:
        read_daily(data)
    message = str(exc.value)
    assert "vanished_rate" in message
    assert "2026-06-01.csv" in message


def test_read_daily_rejects_a_rate_above_one(tmp_path):
    data = write_dataset(
        tmp_path / "data",
        daily_rows=[daily_row("2026-06-01", "R1", scheduled=3, vanished_rate=1.5)],
    )
    with pytest.raises(ValueError) as exc:
        read_daily(data)
    message = str(exc.value)
    assert "vanished_rate" in message
    assert "1.5" in message


def test_read_daily_rejects_a_negative_count(tmp_path):
    data = write_dataset(
        tmp_path / "data",
        daily_rows=[daily_row("2026-06-01", "R1", scheduled=-1)],
    )
    with pytest.raises(ValueError) as exc:
        read_daily(data)
    message = str(exc.value)
    assert "scheduled" in message
    assert "-1" in message


def test_read_daily_rejects_a_non_numeric_cell(tmp_path):
    data = write_dataset(
        tmp_path / "data",
        daily_rows=[daily_row("2026-06-01", "R1", scheduled="not-a-number")],
    )
    with pytest.raises(ValueError) as exc:
        read_daily(data)
    message = str(exc.value)
    assert "scheduled" in message
    assert "not-a-number" in message


def test_read_daily_blank_rate_is_still_none_alongside_validation(tmp_path):
    """The domain check must not turn the one legitimate 'no value' case into
    an error."""
    data = write_dataset(
        tmp_path / "data",
        daily_rows=[daily_row("2026-06-01", "R1", scheduled=3, excluded=3)],
    )
    row = read_daily(data)[0]
    assert row["vanished_rate"] is None


def test_read_daily_accepts_a_real_zero_rate(tmp_path):
    data = write_dataset(
        tmp_path / "data",
        daily_rows=[daily_row("2026-06-01", "R1", scheduled=3, completed=3,
                               vanished_rate="0.000000")],
    )
    row = read_daily(data)[0]
    assert row["vanished_rate"] == 0.0
    assert isinstance(row["vanished_rate"], float)
