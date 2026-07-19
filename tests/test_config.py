from pathlib import Path

import ghostbus_config as cfg


def test_get_db_sets_wal_and_busy_timeout(tmp_path, monkeypatch):
    db_path = tmp_path / "nested" / "ghostbus.db"
    monkeypatch.setenv("GHOSTBUS_DB", str(db_path))
    db = cfg.get_db()
    assert db_path.exists()
    (mode,) = db.execute("PRAGMA journal_mode").fetchone()
    assert mode.lower() == "wal"
    (timeout,) = db.execute("PRAGMA busy_timeout").fetchone()
    assert timeout == 30000


def test_get_db_explicit_path_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GHOSTBUS_DB", str(tmp_path / "env.db"))
    explicit = tmp_path / "explicit.db"
    cfg.get_db(str(explicit))
    assert explicit.exists()
    assert not (tmp_path / "env.db").exists()


def test_get_db_default_path_when_unset(monkeypatch):
    monkeypatch.delenv("GHOSTBUS_DB", raising=False)
    assert cfg.DEFAULT_DB_PATH == "state/ghostbus.db"


def test_read_nta_api_key(monkeypatch):
    monkeypatch.delenv("NTA_API_KEY", raising=False)
    assert cfg.read_nta_api_key() is None
    monkeypatch.setenv("NTA_API_KEY", "secret")
    assert cfg.read_nta_api_key() == "secret"


def test_read_archive_dir_default(monkeypatch):
    monkeypatch.delenv("GHOSTBUS_ARCHIVE", raising=False)
    assert cfg.read_archive_dir() == Path("state/archive")


def test_read_archive_dir_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom-archive"
    monkeypatch.setenv("GHOSTBUS_ARCHIVE", str(custom))
    assert cfg.read_archive_dir() == custom


def test_read_agency_names_default(monkeypatch):
    monkeypatch.delenv("GHOSTBUS_AGENCIES", raising=False)
    assert cfg.read_agency_names() == {"Dublin Bus", "Go-Ahead Ireland"}


def test_read_agency_names_override(monkeypatch):
    monkeypatch.setenv("GHOSTBUS_AGENCIES", "Fixtureville Bus, Go-Ahead Fixtureville")
    assert cfg.read_agency_names() == {"Fixtureville Bus", "Go-Ahead Fixtureville"}


def test_read_agency_names_strips_blanks(monkeypatch):
    monkeypatch.setenv("GHOSTBUS_AGENCIES", "Dublin Bus,,  ")
    assert cfg.read_agency_names() == {"Dublin Bus"}


def test_read_match_radius_default(monkeypatch):
    monkeypatch.delenv("GHOSTBUS_MATCH_RADIUS_M", raising=False)
    assert cfg.read_match_radius_m() == 250.0


def test_read_match_radius_override(monkeypatch):
    monkeypatch.setenv("GHOSTBUS_MATCH_RADIUS_M", "150")
    assert cfg.read_match_radius_m() == 150.0
