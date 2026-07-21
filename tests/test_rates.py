"""Wilson score intervals: hand-computed values, edge cases, and invariants.

Every expected number in this file was computed from the formula in
docs/superpowers/specs/2026-07-19-publisher-design.md:

    p      = k / n
    denom  = 1 + z**2 / n
    centre = (p + z**2 / (2n)) / denom
    margin = z * sqrt( p(1-p)/n + z**2 / (4n**2) ) / denom

with z = 1.96. Worked example for k=2, n=30 (z**2 = 3.8416):

    p      = 2/30                          = 0.06666666...
    denom  = 1 + 3.8416/30                 = 1.12805333...
    centre = (0.06666667 + 0.06402667) / 1.12805333   = 0.11585740...
    margin = 1.96 * sqrt(0.00207407 + 0.00106711) / 1.12805333
           = 1.96 * 0.05604631 / 1.12805333            = 0.09738077...
    lo     = 0.11585740 - 0.09738077       = 0.01847663...
    hi     = 0.11585740 + 0.09738077       = 0.21323817...
"""
import pytest

from aggregate.rates import rate_with_interval, wilson_interval


def test_wilson_matches_hand_computed_two_of_thirty():
    lo, hi = wilson_interval(2, 30)
    assert lo == pytest.approx(0.01847663532769335, rel=1e-12)
    assert hi == pytest.approx(0.21323817721072091, rel=1e-12)


def test_wilson_matches_hand_computed_five_of_twenty():
    lo, hi = wilson_interval(5, 20)
    assert lo == pytest.approx(0.11186005278940309, rel=1e-12)
    assert hi == pytest.approx(0.4687050099580636, rel=1e-12)


def test_zero_successes_gives_exactly_zero_lower_bound_and_positive_upper():
    lo, hi = wilson_interval(0, 30)
    assert lo == 0.0
    assert hi > 0.0
    assert hi == pytest.approx(0.113517091390478, rel=1e-12)


def test_all_successes_gives_exactly_one_upper_bound_and_lower_below_one():
    lo, hi = wilson_interval(30, 30)
    assert hi == 1.0
    assert lo < 1.0
    assert lo == pytest.approx(0.8864829086095221, rel=1e-12)


def test_zero_trials_returns_none():
    assert wilson_interval(0, 0) is None
    assert rate_with_interval(0, 0) is None


def test_negative_trials_returns_none():
    # A denominator of scheduled - excluded can only be <= 0 if the data is
    # corrupt; an undefined rate is never reported as 0.0.
    assert wilson_interval(0, -1) is None
    assert rate_with_interval(0, -1) is None


def test_bounds_always_within_zero_and_one():
    for n in range(1, 41):
        for k in range(0, n + 1):
            lo, hi = wilson_interval(k, n)
            assert 0.0 <= lo <= hi <= 1.0, (k, n, lo, hi)


def test_interval_narrows_monotonically_as_n_grows_at_fixed_p():
    # p = 0.1 held constant, n = 10 -> 100 -> 1000.
    widths = []
    for k, n in ((1, 10), (10, 100), (100, 1000)):
        lo, hi = wilson_interval(k, n)
        widths.append(hi - lo)
    assert widths[0] == pytest.approx(0.386280635981851, rel=1e-12)
    assert widths[1] == pytest.approx(0.11913876275452927, rel=1e-12)
    assert widths[2] == pytest.approx(0.03724320594264838, rel=1e-12)
    assert widths[0] > widths[1] > widths[2]


def test_rate_with_interval_returns_point_estimate_first():
    result = rate_with_interval(2, 30)
    assert result is not None
    rate, lo, hi = result
    assert rate == pytest.approx(2 / 30, rel=1e-12)
    assert lo == pytest.approx(0.01847663532769335, rel=1e-12)
    assert hi == pytest.approx(0.21323817721072091, rel=1e-12)
    assert lo <= rate <= hi


def test_rate_with_interval_point_estimate_always_inside_interval():
    for n in range(1, 41):
        for k in range(0, n + 1):
            rate, lo, hi = rate_with_interval(k, n)
            assert lo <= rate <= hi, (k, n, lo, rate, hi)


def test_z_is_configurable_and_wider_z_gives_wider_interval():
    lo95, hi95 = wilson_interval(2, 30)
    lo99, hi99 = wilson_interval(2, 30, z=2.576)
    assert (hi99 - lo99) > (hi95 - lo95)
