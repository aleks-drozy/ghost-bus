import json
import os
from pathlib import Path

import pytest

from tests.site_fixtures import daily_row, uptime_row, write_dataset

from publish.site import (DatasetError, OutputDirError, build_site, leaderboard,
                          read_daily, render_board)
from publish.slugs import slug_map

GOLDEN = Path(__file__).parent / "golden" / "index_board.html"


def day_rows(route_id, scheduled, vanished=0, untracked=0, excluded=0,
             day="2026-06-28", **kw):
    return daily_row(day, route_id, scheduled=scheduled, excluded=excluded,
                     vanished=vanished, untracked=untracked, cancelled=0,
                     completed=scheduled - excluded - vanished - untracked, **kw)


def ready_dataset(tmp_path):
    daily = [
        day_rows("BIG", 200, vanished=8, untracked=4,
                 route_short_name="1", route_long_name="Fixtureville Main",
                 agency_name="Fixtureville Bus"),
        day_rows("SMALL", 30, vanished=2, untracked=1,
                 route_short_name="2", route_long_name="Fixtureville Orbital",
                 agency_name="Fixtureville Bus"),
        day_rows("03C 120 e a", 12, vanished=1,
                 route_short_name="120", route_long_name="Fixtureville Crosstown",
                 agency_name="Go-Ahead Fixtureville"),
    ]
    uptime = [uptime_row("2026-06-28", 1440, 1440), uptime_row("2026-06-26", 1440, 1200)]
    return write_dataset(tmp_path / "data", daily_rows=daily, uptime_rows=uptime,
                         manifest={"coverage": {"first_day": "2026-06-28",
                                                "last_day": "2026-06-28",
                                                "complete_days": 28}})


def test_build_site_writes_every_page(tmp_path):
    data = ready_dataset(tmp_path)
    out = tmp_path / "_site"
    build_site(data, out)
    for name in ("index.html", "methodology.html", "about-data.html", "style.css",
                 "manifest.json"):
        assert (out / name).is_file(), name
    assert (out / "route" / "big.html").is_file()
    assert (out / "route" / "small.html").is_file()
    assert (out / "route" / "03c-120-e-a.html").is_file()


def test_build_site_copies_the_data_tree_so_csv_links_resolve(tmp_path):
    data = ready_dataset(tmp_path)
    out = tmp_path / "_site"
    build_site(data, out)
    assert (out / "data" / "daily" / "2026-06-28.csv").is_file()
    assert (out / "data" / "uptime" / "2026-06-28.csv").is_file()
    about = (out / "about-data.html").read_text(encoding="utf-8")
    for href in ("data/daily/2026-06-28.csv", "data/uptime/2026-06-28.csv"):
        assert f'href="{href}"' in about
        assert (out / href).is_file()


def test_build_site_refuses_an_unexpected_file_in_the_dataset(tmp_path):
    # A blanket copy would serve attacker-written HTML from this site's own
    # origin, riding the legitimate token through the legitimate workflow.
    data = ready_dataset(tmp_path)
    (data / "evil.html").write_text("<p>x</p>", encoding="utf-8")
    with pytest.raises(DatasetError):
        build_site(data, tmp_path / "_site")


def test_build_site_refuses_a_non_csv_under_daily(tmp_path):
    data = ready_dataset(tmp_path)
    (data / "daily" / "evil.html").write_text("<p>x</p>", encoding="utf-8")
    with pytest.raises(DatasetError):
        build_site(data, tmp_path / "_site")


def test_build_site_refuses_a_daily_csv_dated_outside_manifest_coverage(tmp_path):
    """C1(b): the D3-shaped structural guard.

    This module never opens the database, and must not simply trust that the
    daily/ directory and the manifest it was handed still agree - a
    publisher bug that regresses the C1 prune, a partial push, or a manifest
    edited by hand can all leave a daily CSV dated outside the range the
    manifest's own coverage claims. That is exactly what an un-pruned orphan
    CSV looks like from this side of the split, and build_site must refuse to
    render it rather than silently publish a coverage the manifest denies.
    """
    daily = [
        day_rows("BIG", 200, vanished=8, untracked=4,
                 route_short_name="1", route_long_name="Fixtureville Main",
                 agency_name="Fixtureville Bus"),
        day_rows("BIG", 200, vanished=8, untracked=4, day="2026-06-29",
                 route_short_name="1", route_long_name="Fixtureville Main",
                 agency_name="Fixtureville Bus"),
    ]
    data = write_dataset(
        tmp_path / "data", daily_rows=daily,
        uptime_rows=[uptime_row("2026-06-28", 1440, 1440)],
        manifest={"coverage": {"first_day": "2026-06-28", "last_day": "2026-06-28",
                               "complete_days": 28}})
    with pytest.raises(DatasetError):
        build_site(data, tmp_path / "_site")


def test_build_site_refuses_route_data_behind_a_pre_baseline_page(tmp_path):
    data = ready_dataset(tmp_path)
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    manifest["scoreboard_ready"] = False
    (data / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with pytest.raises(DatasetError):
        build_site(data, tmp_path / "_site")


def test_build_site_records_the_slug_map_in_the_manifest(tmp_path):
    data = ready_dataset(tmp_path)
    out = tmp_path / "_site"
    written = build_site(data, out)
    on_disk = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk == written
    assert on_disk["route_slugs"]["03C 120 e a"] == "03c-120-e-a"
    assert on_disk["schema_version"] == 1


def incumbent_dataset(tmp_path):
    """A dataset whose manifest already publishes a slug for an incumbent route.

    "03C 120 e a" sorts before "03C/120/e/a" (0x20 < 0x2F), so a fresh
    assignment would give the bare slug to the newcomer and move the
    incumbent's live URL. The published map is what stops that.
    """
    rows = read_daily(ready_dataset(tmp_path)) + [
        day_rows("03C/120/e/a", 40, vanished=2, route_short_name="120x")]
    return write_dataset(
        tmp_path / "data", daily_rows=rows,
        uptime_rows=[uptime_row("2026-06-28", 1440, 1440)],
        manifest={"coverage": {"first_day": "2026-06-28",
                               "last_day": "2026-06-28", "complete_days": 28},
                  "route_slugs": {"03C/120/e/a": "03c-120-e-a"}})


def test_build_site_honours_the_slug_map_published_in_the_dataset(tmp_path):
    """The dataset decides route URLs; the builder obeys the map it is given."""
    data = incumbent_dataset(tmp_path)
    out = tmp_path / "_site"
    written = build_site(data, out)
    assert written["route_slugs"]["03C/120/e/a"] == "03c-120-e-a"
    assert written["route_slugs"]["03C 120 e a"] == "03c-120-e-a-2"
    assert (out / "route" / "03c-120-e-a.html").is_file()
    assert (out / "route" / "03c-120-e-a-2.html").is_file()


def test_route_urls_are_identical_across_two_fresh_output_directories(tmp_path):
    """Models the ephemeral CI runner, where _site never survives a run.

    The stable-URL guarantee has to come from the dataset, which is checked
    out, and not from the previous build, which on a fresh runner does not
    exist. Two builds must go to two different, previously nonexistent dirs -
    that models a runner that never carries _site between runs.

    Asserting only that the two builds AGREE with each other is not enough:
    slug_map is a pure function of the route-id list, so two builds fed the
    same ids without `existing=` would recompute the same WRONG assignment
    and agree perfectly. That agreement is implied by determinism alone and
    does not distinguish "sourced from the published map" from "recomputed
    every time, coincidentally identical". incumbent_dataset publishes
    "03C/120/e/a" -> "03c-120-e-a"; a fresh recomputation (ids sorted, no
    existing=) would instead give the bare slug to "03C 120 e a" (space
    0x20 sorts before slash 0x2F) and bump the incumbent to "-2". Asserting
    the actual pairing, not just cross-run equality, is what catches that.
    """
    data = incumbent_dataset(tmp_path)

    first = build_site(data, tmp_path / "run-1")
    second = build_site(data, tmp_path / "run-2")

    for out, written in ((tmp_path / "run-1", first), (tmp_path / "run-2", second)):
        assert written["route_slugs"]["03C/120/e/a"] == "03c-120-e-a"
        assert written["route_slugs"]["03C 120 e a"] == "03c-120-e-a-2"
        assert (out / "route" / "03c-120-e-a.html").is_file()
        assert (out / "route" / "03c-120-e-a-2.html").is_file()


def test_build_site_is_idempotent(tmp_path):
    data = ready_dataset(tmp_path)
    out = tmp_path / "_site"
    build_site(data, out)
    first = (out / "index.html").read_text(encoding="utf-8")
    build_site(data, out)
    assert (out / "index.html").read_text(encoding="utf-8") == first


def test_build_site_writes_a_sentinel_marking_the_dir_as_ours(tmp_path):
    data = ready_dataset(tmp_path)
    out = tmp_path / "_site"
    build_site(data, out)
    assert (out / ".ghost-bus-site").is_file()


def test_build_site_can_rebuild_into_its_own_sentinelled_dir(tmp_path):
    """A dir this tool already built into is safe to build into again.

    Rebuilding is the normal, expected case (a dev running the CLI twice
    locally, or CI reusing a workspace between steps of the same job) and
    must not be blocked by the same guard that refuses a foreign directory.
    """
    data = ready_dataset(tmp_path)
    out = tmp_path / "_site"
    build_site(data, out)
    build_site(data, out)  # must not raise
    assert (out / "index.html").is_file()
    assert (out / ".ghost-bus-site").is_file()


def test_build_site_refuses_a_foreign_non_empty_output_dir(tmp_path):
    """Refuses to build into a directory it did not create, and - the
    assertion that matters - does not touch what is already there.

    This is what stops a careless or mistaken --out (including, worst case,
    one that happens to point at the published dataset itself) from silently
    destroying someone else's files: the tool can only tell "safe to
    overwrite" from "not ours" by the sentinel, so anything non-empty without
    one is left completely alone.
    """
    data = ready_dataset(tmp_path)
    out = tmp_path / "_site"
    out.mkdir()
    (out / "index.html").write_text("<p>someone else's site</p>", encoding="utf-8")
    (out / "notes.txt").write_text("do not touch", encoding="utf-8")
    with pytest.raises(OutputDirError):
        build_site(data, out)
    assert (out / "index.html").read_text(encoding="utf-8") == "<p>someone else's site</p>"
    assert (out / "notes.txt").read_text(encoding="utf-8") == "do not touch"
    assert sorted(p.name for p in out.iterdir()) == ["index.html", "notes.txt"]


def test_pre_baseline_build_emits_no_route_pages(tmp_path):
    data = write_dataset(
        tmp_path / "data",
        uptime_rows=[uptime_row("2026-06-09", 1440, 1440)],
        manifest={"scoreboard_ready": False,
                  "coverage": {"first_day": "2026-06-01", "last_day": "2026-06-09",
                               "complete_days": 9}},
    )
    out = tmp_path / "_site"
    build_site(data, out)
    assert not (out / "route").exists()
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "<table" not in index
    assert "day 9 of 14" in index
    assert (out / "methodology.html").is_file()
    assert (out / "about-data.html").is_file()
    assert "uptime-strip" in index


def test_output_files_are_utf8(tmp_path):
    data = ready_dataset(tmp_path)
    out = tmp_path / "_site"
    build_site(data, out)
    text = (out / "index.html").read_bytes().decode("utf-8")
    assert "—" in text or "–" in text


def test_leaderboard_html_matches_the_golden(tmp_path):
    """Golden HTML for the board fragment.

    To regenerate after an intentional markup change:
        GHOSTBUS_UPDATE_GOLDEN=1 python -m pytest tests/test_site_build.py -q
    then read the diff before committing it.
    """
    data = ready_dataset(tmp_path)
    rows = read_daily(data)
    ranked, unranked = leaderboard(rows)
    slugs = slug_map(e["route_id"] for e in ranked + unranked)
    got = render_board(ranked, unranked, slugs)

    if os.environ.get("GHOSTBUS_UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(got, encoding="utf-8")

    assert GOLDEN.is_file(), "golden missing; regenerate with GHOSTBUS_UPDATE_GOLDEN=1"
    assert got == GOLDEN.read_text(encoding="utf-8")


def test_golden_pins_the_facts_that_matter(tmp_path):
    """Belt and braces: the golden could be regenerated wrong, these cannot."""
    data = ready_dataset(tmp_path)
    rows = read_daily(data)
    ranked, unranked = leaderboard(rows)
    slugs = slug_map(e["route_id"] for e in ranked + unranked)
    got = render_board(ranked, unranked, slugs)

    assert got.index(">1<") < got.index(">2<")          # BIG ranked above SMALL
    assert "Not enough data yet" in got
    assert got.index("Not enough data yet") < got.index("120")
    assert "4.0%" in got and "6.7%" in got              # both point estimates shown
    # Per-row sums that must never appear: BIG 4.0+2.0, SMALL 6.7+3.3.
    assert "6.0%" not in got
    assert "10.0%" not in got
    assert 'href="route/03c-120-e-a.html"' in got
