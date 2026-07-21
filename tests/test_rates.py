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


def test_zero_successes_gives_exactly_zero_for_problematic_n_values():
    # Floating-point precision issue: for k=0, centre - margin can be ~1e-17
    # instead of exactly 0.0 for certain n values. Explicit clamping is required.
    for n in (11, 22, 27):
        lo, hi = wilson_interval(0, n)
        assert lo == 0.0, f"Expected exactly 0.0 for n={n}, got {repr(lo)}"


def test_all_successes_gives_exactly_one_for_problematic_n_values():
    # Floating-point precision issue: for k=n, centre + margin can be slightly
    # less than 1.0 (e.g., 0.9999999999999999) instead of exactly 1.0 for
    # certain n values. Explicit clamping is required.
    for n in (6, 21, 31, 38):
        lo, hi = wilson_interval(n, n)
        assert hi == 1.0, f"Expected exactly 1.0 for n={n}, got {repr(hi)}"


# --- The never-summed invariant (design decision D1) -------------------------
#
# VANISHED and UNTRACKED are separate claims and are published separately. No
# row emitted by the rollup - and, by extension, no CSV column or table cell
# built from one - may carry their sum. This test is the pin: any future code
# that reintroduces a combined rate must fail here.

import sqlite3  # noqa: E402  (kept next to the invariant it supports)

from aggregate.rollup import RATE_KEYS, route_day_rollup  # noqa: E402


def _invariant_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
    CREATE TABLE trip_outcomes (
      trip_id TEXT, service_date TEXT, route_id TEXT, start_utc TEXT, outcome TEXT,
      PRIMARY KEY (trip_id, service_date));
    """)
    rows = [
        # R1: scheduled 6, excluded 1 -> denominator 5, 2 VANISHED, 1 UNTRACKED,
        # 2 COMPLETED, 0 CANCELLED. Forbidden values: count_sum=3, rate=0.6,
        # pct=60.0; none match any legitimate field on this row.
        ("a", "2026-03-23", "R1", "2026-03-23T07:00:00+00:00", "COMPLETED"),
        ("b", "2026-03-23", "R1", "2026-03-23T07:30:00+00:00", "COMPLETED"),
        ("c", "2026-03-23", "R1", "2026-03-23T08:00:00+00:00", "VANISHED"),
        ("d", "2026-03-23", "R1", "2026-03-23T08:30:00+00:00", "VANISHED"),
        ("e", "2026-03-23", "R1", "2026-03-23T09:00:00+00:00", "UNTRACKED"),
        ("f", "2026-03-23", "R1", "2026-03-23T09:30:00+00:00", "EXCLUDED"),
        # R2: every trip excluded -> denominator 0, all rates None.
        ("g", "2026-03-23", "R2", "2026-03-23T10:00:00+00:00", "EXCLUDED"),
        # R3: scheduled 5, 1 VANISHED, 3 UNTRACKED, 1 COMPLETED, 0 CANCELLED.
        # Forbidden values: count_sum=4, rate=0.8, pct=80.0; catches 1.0 clamping.
        ("h", "2026-03-24", "R3", "2026-03-24T07:00:00+00:00", "COMPLETED"),
        ("i", "2026-03-24", "R3", "2026-03-24T07:30:00+00:00", "VANISHED"),
        ("j", "2026-03-24", "R3", "2026-03-24T08:00:00+00:00", "UNTRACKED"),
        ("k", "2026-03-24", "R3", "2026-03-24T08:30:00+00:00", "UNTRACKED"),
        ("l", "2026-03-24", "R3", "2026-03-24T09:00:00+00:00", "UNTRACKED"),
        # R4: scheduled 2, 2 COMPLETED, 0 VANISHED, 0 UNTRACKED. Exercises the
        # combined==0.0 branch (both rates legitimately 0.0).
        ("m", "2026-03-25", "R4", "2026-03-25T07:00:00+00:00", "COMPLETED"),
        ("n", "2026-03-25", "R4", "2026-03-25T07:30:00+00:00", "COMPLETED"),
    ]
    conn.executemany("INSERT INTO trip_outcomes VALUES (?,?,?,?,?)", rows)
    conn.commit()
    return conn


def test_no_combined_rate_key_exists():
    for row in route_day_rollup(_invariant_db()):
        for key in row:
            assert "ghost" not in key, f"combined-rate key reappeared: {key}"
            assert "combined" not in key, f"combined-rate key reappeared: {key}"
            assert "failure_rate" not in key, f"combined-rate key reappeared: {key}"


def test_published_rate_keys_are_exactly_the_six_split_fields():
    assert RATE_KEYS == ("vanished_rate", "vanished_lo", "vanished_hi",
                         "untracked_rate", "untracked_lo", "untracked_hi")
    for row in route_day_rollup(_invariant_db()):
        rate_like = {k for k in row if k.endswith(("_rate", "_lo", "_hi"))}
        assert rate_like == set(RATE_KEYS), rate_like


def test_no_published_field_equals_the_sum_of_the_two_rates():
    # Fixture is chosen so that every forbidden value is distinct from every
    # published field, preventing false positives. A future editor should not
    # casually change the trip counts without re-verifying.
    # Skip outcome-count keys (vanished, untracked, completed, etc.) and identifiers.
    outcome_keys = {"vanished", "untracked", "completed", "cancelled", "scheduled", "excluded",
                    "route_id", "service_date", "local_hour"}
    for row in route_day_rollup(_invariant_db()):
        denom = row["scheduled"] - row["excluded"]
        if denom <= 0:
            for key in RATE_KEYS:
                assert row[key] is None, key
            continue
        combined = (row["vanished"] + row["untracked"]) / denom
        assert row["vanished_rate"] + row["untracked_rate"] == pytest.approx(combined)
        # Forbidden values: raw rate, percentage form, rounded versions, and count sum.
        # These represent ways the combined rate could leak into published fields.
        forbidden = {
            combined,  # Raw rate (e.g., 0.6)
            combined * 100,  # Percentage (e.g., 60.0)
            round(combined, 1),  # Rounded to 1 decimal
            round(combined, 2),  # Rounded to 2 decimals
            round(combined, 3),  # Rounded to 3 decimals
            round(combined * 100, 1),  # Percentage rounded to 1 decimal
            round(combined * 100, 2),  # Percentage rounded to 2 decimals
            round(combined * 100, 3),  # Percentage rounded to 3 decimals
            row["vanished"] + row["untracked"],  # Integer count sum
        }
        for key, value in row.items():
            # Skip outcome counts, identifiers, None values, and the split rate fields.
            if key in outcome_keys or key in RATE_KEYS or value is None:
                continue
            # Reject any published field that matches a forbidden value.
            if isinstance(value, float):
                for forbidden_val in forbidden:
                    if isinstance(forbidden_val, float):
                        assert value != pytest.approx(forbidden_val), (
                            f"{key} on route {row['route_id']} equals vanished+untracked "
                            f"({combined}); the two rates must never be summed (D1)")
            else:
                # Integer or other type: direct comparison.
                assert value not in forbidden, (
                    f"{key} on route {row['route_id']} has value {value} matching "
                    f"count sum {row['vanished'] + row['untracked']}; "
                    f"the two rates must never be summed (D1)")


def test_the_two_rates_are_reported_independently():
    rows = {r["route_id"]: r for r in route_day_rollup(_invariant_db())}
    r3 = rows["R3"]
    # Different numerators over the same denominator: proof they are not one
    # number wearing two names. R3: denom=5, vanished=1, untracked=3.
    assert r3["vanished_rate"] == pytest.approx(1 / 5)
    assert r3["untracked_rate"] == pytest.approx(3 / 5)
    assert r3["vanished_lo"] != pytest.approx(r3["untracked_lo"])
    assert r3["vanished_hi"] != pytest.approx(r3["untracked_hi"])
