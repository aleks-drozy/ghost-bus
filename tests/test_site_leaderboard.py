import pytest

from tests.site_fixtures import daily_row

from publish.site import (MIN_TRIPS, WINDOW_DAYS, aggregate_window, leaderboard,
                          window_dates)


def days(n, start_day=1):
    return [f"2026-06-{start_day + i:02d}" for i in range(n)]


def spread(route_id, per_day, scheduled, vanished=0, untracked=0, excluded=0, **kw):
    """per_day rows for one route, counts split evenly across the days given."""
    rows = []
    for i, day in enumerate(per_day):
        def share(total):
            base, rem = divmod(total, len(per_day))
            return base + (1 if i < rem else 0)
        rows.append(daily_row(
            day, route_id,
            scheduled=share(scheduled), excluded=share(excluded),
            vanished=share(vanished), untracked=share(untracked),
            cancelled=0,
            completed=share(scheduled) - share(excluded) - share(vanished) - share(untracked),
            **kw,
        ))
    return rows


def test_window_dates_keeps_only_the_last_28_days():
    rows = [daily_row(d, "R1", scheduled=1) for d in days(30)]
    got = window_dates(rows)
    assert len(got) == WINDOW_DAYS
    assert got[0] == "2026-06-03" and got[-1] == "2026-06-30"


def test_aggregate_window_ignores_days_outside_the_window():
    rows = [daily_row(d, "R1", scheduled=1, vanished=1) for d in days(30)]
    entry = aggregate_window(rows)[0]
    assert entry["scheduled"] == 28
    assert entry["days"] == 28


def test_trials_is_scheduled_minus_excluded():
    rows = spread("R1", days(1), scheduled=40, excluded=10, vanished=3)
    entry = aggregate_window(rows)[0]
    assert entry["trials"] == 30


def test_route_with_29_judged_trips_is_unranked_and_30_is_ranked():
    rows = spread("R29", days(1), scheduled=29, vanished=2) + \
           spread("R30", days(1), scheduled=30, vanished=2)
    ranked, unranked = leaderboard(rows)
    assert [e["route_id"] for e in ranked] == ["R30"]
    assert [e["route_id"] for e in unranked] == ["R29"]
    assert MIN_TRIPS == 30


def test_high_exclusion_route_is_unranked_even_with_30_scheduled():
    """The gate reads trials, not scheduled.

    30 scheduled, 29 excluded, 1 vanished: one observation, a 100% vanished
    rate, and a Wilson lower bound of 0.2065 - higher than any real route's.
    Gating on `scheduled` would put a one-observation route at the top of a
    public list of the worst routes.
    """
    rows = spread("MOSTLY_BLIND", days(1), scheduled=30, excluded=29, vanished=1)
    ranked, unranked = leaderboard(rows)
    assert [e["route_id"] for e in ranked] == []
    assert unranked[0]["trials"] == 1
    assert unranked[0]["vanished_interval"][0] == pytest.approx(1.0)
    # Verified against a 50-digit decimal closed-form Wilson derivation:
    # exact lo = 0.20654329147389292795... Do not "correct" this back to
    # 0.20653997 - that figure was a hand-computation error in the original
    # plan, off by 3.3e-6, caught by this test itself.
    assert unranked[0]["vanished_interval"][1] == pytest.approx(0.20654329147389294, abs=1e-6)


def test_route_with_no_trials_is_unranked_and_has_no_interval():
    rows = spread("RALL", days(1), scheduled=40, excluded=40)
    ranked, unranked = leaderboard(rows)
    assert ranked == []
    assert unranked[0]["vanished_interval"] is None
    assert unranked[0]["untracked_interval"] is None


def test_ranking_follows_the_lower_bound_not_the_point_estimate():
    """Pinned disagreement case.

    SMALL: 2 vanished of 30 -> rate 6.67%, Wilson lower bound 1.8477%
    BIG:   8 vanished of 200 -> rate 4.00%, Wilson lower bound 2.0405%

    BIG has the LOWER headline rate and the HIGHER lower bound, so ranking by
    the lower bound must put BIG first. If this test ever passes with SMALL
    first, the board is ranking point estimates and D2 has been broken.
    """
    rows = spread("SMALL", days(1), scheduled=30, vanished=2) + \
           spread("BIG", days(1), scheduled=200, vanished=8)
    ranked, _ = leaderboard(rows)
    small = next(e for e in ranked if e["route_id"] == "SMALL")
    big = next(e for e in ranked if e["route_id"] == "BIG")

    assert small["vanished_interval"][0] == pytest.approx(0.06666667, abs=1e-8)
    assert big["vanished_interval"][0] == pytest.approx(0.04, abs=1e-8)
    assert small["vanished_interval"][1] == pytest.approx(0.01847664, abs=1e-8)
    # Verified against a 50-digit decimal closed-form Wilson derivation:
    # exact lo = 0.02040538715065640445... Do not "correct" this back to
    # 0.02040540 - that figure was a rounding slip in the original plan, off
    # by 1.3e-8 (exceeds this assertion's own abs=1e-8 tolerance).
    assert big["vanished_interval"][1] == pytest.approx(0.02040538715065641, abs=1e-8)
    assert small["vanished_interval"][0] > big["vanished_interval"][0]
    assert small["vanished_interval"][1] < big["vanished_interval"][1]

    assert [e["route_id"] for e in ranked] == ["BIG", "SMALL"]


def test_untracked_never_affects_rank():
    rows = spread("A", days(1), scheduled=100, vanished=5, untracked=0) + \
           spread("B", days(1), scheduled=100, vanished=5, untracked=50)
    ranked, _ = leaderboard(rows)
    assert [e["route_id"] for e in ranked] == ["A", "B"]  # tie broken by route_id only


def test_no_entry_field_equals_vanished_plus_untracked():
    rows = spread("R1", days(1), scheduled=100, vanished=7, untracked=11)
    entry = aggregate_window(rows)[0]
    combined = entry["vanished"] + entry["untracked"]
    assert combined == 18
    numeric = {k: v for k, v in entry.items() if isinstance(v, int)}
    assert all(v != combined for k, v in numeric.items()
               if k not in ("vanished", "untracked"))
    assert not any("ghost" in k or "combined" in k or "total_rate" in k for k in entry)


def test_names_are_carried_through_from_the_csv():
    rows = spread("03C 120 e a", days(1), scheduled=30, vanished=1,
                  route_short_name="120", route_long_name="Main Street",
                  agency_name="Dublin Bus")
    entry = aggregate_window(rows)[0]
    assert entry["route_short_name"] == "120"
    assert entry["agency_name"] == "Dublin Bus"


def test_unranked_routes_are_ordered_by_judged_trips_then_id():
    rows = spread("A", days(1), scheduled=5) + spread("B", days(1), scheduled=20)
    _, unranked = leaderboard(rows)
    assert [e["route_id"] for e in unranked] == ["B", "A"]
