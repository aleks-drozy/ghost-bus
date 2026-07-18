import sqlite3

from tests.fixtureville import build_gtfs_zip
from timetable.refresh import refresh


def test_refresh_writes_zip_loads_and_summarizes(tmp_path):
    source_zip = tmp_path / "source.zip"
    build_gtfs_zip(source_zip)
    raw = source_zip.read_bytes()

    db = sqlite3.connect(":memory:")
    dest = tmp_path / "state" / "gtfs_static.zip"
    summary = refresh(db, fetch_fn=lambda: raw, dest_zip_path=dest)

    assert dest.exists()
    assert dest.read_bytes() == raw
    assert len(summary["gtfs_hash"]) == 64
    assert summary["n_trips"] == 19  # 11 R1 + 5 R2 wk + 1 R2 sat + 2 R3 wk
    assert set(summary["agencies"]) == {"Fixtureville Bus", "Go-Ahead Fixtureville"}

    # A second refresh against the same db (fresh timetable version) must not
    # duplicate rows - load_gtfs is DELETE-before-insert.
    summary2 = refresh(db, fetch_fn=lambda: raw, dest_zip_path=dest)
    assert summary2["n_trips"] == 19


def test_refresh_creates_missing_parent_dir(tmp_path):
    source_zip = tmp_path / "source.zip"
    build_gtfs_zip(source_zip)
    raw = source_zip.read_bytes()

    db = sqlite3.connect(":memory:")
    dest = tmp_path / "nested" / "does" / "not" / "exist" / "gtfs.zip"
    refresh(db, fetch_fn=lambda: raw, dest_zip_path=dest)
    assert dest.exists()
