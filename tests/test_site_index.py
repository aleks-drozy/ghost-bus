from tests.site_fixtures import DEFAULT_MANIFEST, daily_row, uptime_row

from publish.site import (SITE_DIR, fmt_rate, leaderboard, render_index,
                          render_uptime_strip)
from publish.slugs import slug_map


def one_day(route_id, scheduled, vanished=0, untracked=0, excluded=0,
            day="2026-06-28", **kw):
    return daily_row(day, route_id, scheduled=scheduled, excluded=excluded,
                     vanished=vanished, untracked=untracked, cancelled=0,
                     completed=scheduled - excluded - vanished - untracked, **kw)


def build(daily_rows, uptime_rows=(), manifest=None):
    man = dict(DEFAULT_MANIFEST)
    if manifest:
        man.update(manifest)
    ranked, unranked = leaderboard(list(daily_rows))
    slugs = slug_map(e["route_id"] for e in ranked + unranked)
    return render_index(SITE_DIR, man, list(daily_rows), list(uptime_rows),
                        ranked, unranked, slugs)


def test_ranked_table_lists_routes_worst_lower_bound_first():
    html = build([one_day("SMALL", 30, vanished=2), one_day("BIG", 200, vanished=8)])
    assert html.index(">BIG<") < html.index(">SMALL<")


def test_every_ranked_row_shows_its_sample_size():
    html = build([one_day("R1", 40, excluded=10, vanished=3)])
    assert "Trips judged" in html
    assert 'title="40 scheduled, 10 excluded"' in html
    assert ">30<" in html


def test_untracked_has_its_own_column_and_interval():
    html = build([one_day("R1", 100, vanished=5, untracked=20)])
    assert html.count("95% interval") == 2
    assert "Untracked" in html and "Vanished" in html


def test_below_threshold_routes_appear_separately_with_counts():
    html = build([one_day("R1", 100, vanished=5), one_day("TINY", 12, vanished=1)])
    assert "Not enough data yet" in html
    assert html.index("TINY") > html.index("Not enough data yet")
    assert ">12<" in html


def test_board_copy_matches_the_gate_the_code_enforces():
    html = build([one_day("R1", 100, vanished=5), one_day("TINY", 12, vanished=1)])
    assert "30 trips we could judge" in html
    assert "30 scheduled trips" not in html


def test_zero_trial_route_renders_an_em_dash_not_zero():
    rows = [one_day("ALLX", 40, excluded=40)]
    ranked, unranked = leaderboard(rows)
    assert ranked == []
    # The honest formatting of an undefined rate, asserted at the source.
    assert fmt_rate(unranked[0]["vanished_interval"]) == "—"
    html = build(rows)
    assert "0.0%" not in html
    assert ">0<" in html          # the counts are still shown


def test_window_line_states_the_days_actually_behind_it():
    rows = [one_day("R1", 4, vanished=1, day=f"2026-06-{d:02d}")
            for d in range(1, 15)]
    html = build(rows)
    assert "Rolling 14 complete service days" in html
    assert "Rolling 28" not in html


def test_pre_baseline_mode_emits_no_route_table():
    html = build([], uptime_rows=[uptime_row("2026-06-09")],
                 manifest={"scoreboard_ready": False,
                           "coverage": {"first_day": "2026-06-01",
                                        "last_day": "2026-06-09",
                                        "complete_days": 9}})
    assert "<table" not in html
    assert "collecting baseline" in html.lower()
    assert "day 9 of 14" in html
    assert "uptime-strip" in html


def test_uptime_strip_is_rendered_even_before_baseline():
    html = build([], uptime_rows=[uptime_row("2026-06-09", 1440, 1440)],
                 manifest={"scoreboard_ready": False,
                           "coverage": {"first_day": "2026-06-09",
                                        "last_day": "2026-06-09",
                                        "complete_days": 1}})
    assert "uptime-strip" in html
    assert 'class="day ok"' in html


def test_missing_day_renders_as_a_visible_gap_never_interpolated():
    rows = [uptime_row("2026-06-26", 1440, 1440), uptime_row("2026-06-28", 1440, 1440)]
    strip = render_uptime_strip(rows, "2026-06-28")
    assert 'title="2026-06-27: no data"' in strip
    assert strip.count("day gap") == 28  # 30 cells, 2 of which have data
    assert "2026-06-27: 100.0%" not in strip


def test_uptime_strip_always_has_30_cells():
    strip = render_uptime_strip([uptime_row("2026-06-28")], "2026-06-28")
    assert strip.count("<li") == 30


def test_uptime_strip_classes_reflect_the_fraction():
    rows = [
        uptime_row("2026-06-28", 1440, 1440),
        uptime_row("2026-06-27", 1440, 1360),
        uptime_row("2026-06-26", 1440, 900),
    ]
    strip = render_uptime_strip(rows, "2026-06-28")
    assert 'class="day ok"' in strip
    assert 'class="day degraded"' in strip
    assert 'class="day down"' in strip


def test_index_links_to_the_route_page_by_slug():
    html = build([one_day("03C 120 e a", 40, vanished=2, route_short_name="120")])
    assert 'href="route/03c-120-e-a.html"' in html
