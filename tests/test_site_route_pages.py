from tests.site_fixtures import DEFAULT_MANIFEST, daily_row

from publish.site import SITE_DIR, leaderboard, render_route
from publish.slugs import slug_map


def rows_for(route_id, days_and_counts, **kw):
    out = []
    for day, scheduled, vanished, untracked in days_and_counts:
        out.append(daily_row(day, route_id, scheduled=scheduled, excluded=0,
                             vanished=vanished, untracked=untracked, cancelled=0,
                             completed=scheduled - vanished - untracked, **kw))
    return out


def page_for(rows, route_id):
    ranked, unranked = leaderboard(rows)
    entries = ranked + unranked
    slugs = slug_map(e["route_id"] for e in entries)
    entry = next(e for e in entries if e["route_id"] == route_id)
    position = ranked.index(entry) + 1 if entry in ranked else None
    return render_route(SITE_DIR, DEFAULT_MANIFEST, entry, rows, slugs, position)


def test_route_page_shows_names_agency_and_raw_route_id():
    rows = rows_for("03C 120 e a", [("2026-06-28", 40, 2, 3)],
                    route_short_name="120", route_long_name="Main Street",
                    agency_name="Dublin Bus")
    html = page_for(rows, "03C 120 e a")
    assert "Route 120" in html
    assert "Main Street" in html
    assert "Dublin Bus" in html
    assert "<code>03C 120 e a</code>" in html


def test_route_page_shows_both_rates_with_intervals_and_never_a_sum():
    rows = rows_for("R1", [("2026-06-28", 100, 5, 20)])
    html = page_for(rows, "R1")
    assert "5.0%" in html and "20.0%" in html
    assert "25.0%" not in html
    assert html.count("95% interval") == 2


def test_route_page_states_its_rank_when_ranked():
    rows = rows_for("R1", [("2026-06-28", 100, 5, 0)])
    html = page_for(rows, "R1")
    assert "ranked #1" in html.lower()


def test_unranked_route_page_says_why_it_is_unranked():
    rows = rows_for("TINY", [("2026-06-28", 12, 1, 0)])
    html = page_for(rows, "TINY")
    assert "not ranked" in html.lower()
    assert "30 trips we could judge" in html


def test_unranked_route_page_claims_no_headline_rate():
    """The index says no rate is claimed for these routes. The detail page
    must not then print one - that figure is what gets screenshotted."""
    rows = rows_for("TINY", [("2026-06-28", 12, 1, 0)])
    html = page_for(rows, "TINY")
    assert "8.3%" not in html           # the point estimate is withheld
    assert "—" in html                  # shown as explicitly withheld
    assert "1 trips" in html            # the count is still shown
    assert "1.5–35.4%" in html          # the interval still is too


def test_day_by_day_table_shows_a_gap_row_for_a_missing_day():
    rows = rows_for("R1", [("2026-06-26", 40, 2, 0), ("2026-06-28", 40, 2, 0)])
    html = page_for(rows, "R1")
    assert 'class="gap"' in html
    assert "2026-06-27" in html
    assert "no data published for this day" in html


def test_gap_row_is_never_given_numbers():
    rows = rows_for("R1", [("2026-06-26", 40, 2, 0), ("2026-06-28", 40, 2, 0)])
    html = page_for(rows, "R1")
    gap_start = html.index('class="gap"')
    gap_row = html[gap_start:html.index("</tr>", gap_start)]
    assert "%" not in gap_row


def test_day_table_carries_counts_only_never_per_day_percentages():
    """One service day is a tiny sample by construction, so no rate is
    published at that grain."""
    rows = rows_for("R1", [("2026-06-28", 4, 2, 0)])
    html = page_for(rows, "R1")
    table = html[html.index('<table class="days"'):]
    assert "%" not in table


def test_zero_trial_route_renders_em_dash_not_zero():
    rows = [daily_row("2026-06-28", "R1", scheduled=40, excluded=40, cancelled=0,
                      completed=0, vanished=0, untracked=0)]
    html = page_for(rows, "R1")
    assert "—" in html
    assert "0.0%" not in html


def test_route_page_links_back_with_relative_paths():
    rows = rows_for("R1", [("2026-06-28", 40, 2, 0)])
    html = page_for(rows, "R1")
    assert 'href="../index.html"' in html
    assert 'href="../methodology.html"' in html
    assert 'href="../style.css"' in html
