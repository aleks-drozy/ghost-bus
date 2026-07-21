import datetime as dt

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


def test_gap_row_distinguishes_unscheduled_service_from_nothing_published():
    """I1: a day the site DID publish (some other route has a row for it),
    but THIS route had no scheduled service, must read differently from a
    day nothing was published for at all.

    Before this fix, a route with no Sunday service showed "no data
    published for this day" on every Sunday, on the SAME site whose index
    page uptime strip shows 100% tracker uptime for that same date - two
    pages contradicting each other about the same calendar day. window_dates
    already exposes exactly the distinction needed: if the day is in it,
    something was published and this route simply wasn't running; only a day
    absent from it altogether means nothing was published at all.
    """
    r1_rows = rows_for("R1", [("2026-06-26", 40, 2, 0), ("2026-06-27", 40, 2, 0),
                              ("2026-06-28", 40, 2, 0)])
    r2_rows = rows_for("R2", [("2026-06-26", 40, 2, 0), ("2026-06-28", 40, 2, 0)])
    html = page_for(r1_rows + r2_rows, "R2")
    assert "2026-06-27" in html
    assert "no scheduled service for this route on this day" in html
    assert "no data published for this day" not in html


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


def _gapped_days_and_counts():
    """28 service days scattered across a 40-calendar-day span.

    05-01..05-14 (14 days) then a 12-day publisher gap (05-15..05-26) then
    05-27..06-09 (14 days) = 28 service days total, but the first and last
    of them are 40 calendar days apart. A count-only heading ("Last 28
    complete service days") is true and misleading at once: it reads like
    "the last month or so" when the evidence actually reaches back further.
    """
    start = dt.date(2026, 5, 1)
    gap_start = dt.date(2026, 5, 15)
    gap_end = dt.date(2026, 5, 26)
    end = dt.date(2026, 6, 9)
    days = []
    day = start
    while day <= end:
        if day < gap_start or day > gap_end:
            days.append(day.isoformat())
        day += dt.timedelta(days=1)
    assert len(days) == 28
    assert days[0] == "2026-05-01"
    assert days[-1] == "2026-06-09"
    return [(d, 40, 2, 0) for d in days]


def test_window_heading_states_the_true_span_across_a_gapped_window():
    """The whole point of this test: count alone would pass either way.

    A contiguous 28-day window would make 'Last 28 complete service days'
    and 'spans 2026-05-01 to 2026-06-09' equivalent claims. Only a gapped
    window - where 28 service days stretch across 40 calendar days -
    distinguishes 'states the count' from 'states the count AND the true
    span', which is the honesty this change is for.
    """
    rows = rows_for("R1", _gapped_days_and_counts())
    html = page_for(rows, "R1")
    assert "Rolling 28 complete service days, 2026-05-01 to 2026-06-09." in html


def test_window_heading_states_a_single_day_without_a_bogus_span():
    rows = rows_for("R1", [("2026-06-28", 40, 2, 0)])
    html = page_for(rows, "R1")
    assert "Rolling 1 complete service day, 2026-06-28." in html
    # Not "2026-06-28 to 2026-06-28" - a single day has no span to state.
    assert "2026-06-28 to 2026-06-28" not in html


def test_window_heading_wording_matches_the_index_page():
    """M3: the index and a route page describe the identical fact (the same
    window_dates over the same daily_rows) and must use the same word for
    it - 'Rolling', not 'Last' on one page and 'Rolling' on the other."""
    rows = rows_for("R1", [("2026-06-28", 40, 2, 0)])
    html = page_for(rows, "R1")
    assert "Last 1 complete service day" not in html
    assert "Rolling 1 complete service day" in html


def test_window_heading_states_zero_days_sensibly_when_daily_rows_is_empty():
    """The pre-baseline / defensive path: no complete service days at all.

    Reachable in practice only if render_route is ever called with a window
    that carries no rows for any route (daily_rows empty). Guards against
    a grammatically odd 'Last 0 complete service days, no complete days yet'.
    """
    rows = rows_for("R1", [("2026-06-28", 40, 2, 0)])
    ranked, unranked = leaderboard(rows)
    entries = ranked + unranked
    slugs = slug_map(e["route_id"] for e in entries)
    entry = next(e for e in entries if e["route_id"] == "R1")
    html = render_route(SITE_DIR, DEFAULT_MANIFEST, entry, [], slugs, position=1)
    assert "No complete service days published yet" in html
    assert "Last 0" not in html
