import pytest

from classify.progress import haversine_m, matched_max_seq

# 5 stops on a meridian, 400.3 m apart (mirrors Fixtureville geometry).
STOPS = [(i + 1, 53.3000 + 0.0036 * i, -6.2000) for i in range(5)]
RADIUS = 250.0


def test_haversine_known_distance():
    # 0.0036 deg latitude on a meridian = 0.0036/180*pi*6371000 = 400.3 m
    d = haversine_m(53.3000, -6.2000, 53.3036, -6.2000)
    assert d == pytest.approx(400.3, abs=0.5)


def test_ping_at_stop_matches_it():
    assert matched_max_seq(STOPS, [(53.3001, -6.2000)], RADIUS) == 1  # ~11 m from S1


def test_ping_far_from_route_matches_nothing():
    assert matched_max_seq(STOPS, [(53.5000, -6.2000)], RADIUS) is None  # ~22 km


def test_nearest_stop_wins_not_highest_sequence():
    # 177.9 m from stop 1, 222.4 m from stop 2 - BOTH within 250 m. Correct
    # nearest-stop matching credits 1; sloppy max-seq-in-radius would say 2.
    assert matched_max_seq(STOPS, [(53.3016, -6.2000)], RADIUS) == 1


def test_equidistant_tie_credits_lower_sequence():
    # A loop route visits the same physical stop at seq 2 and seq 6, so a ping
    # there is EXACTLY equidistant from both entries (identical coordinates -
    # a float-midpoint "tie" is not bitwise-equal and tests nothing). The
    # higher-sequence entry is listed first so this fails if the tie-break
    # clause is dropped: credit the LOWER sequence, never over-credit.
    _, s2_lat, s2_lon = STOPS[1]  # reuse the exact floats: a retyped literal
    loop = [(6, s2_lat, s2_lon)] + STOPS  # differs in the last ulp and unties
    assert matched_max_seq(loop, [(s2_lat, s2_lon)], RADIUS) == 2


def test_max_over_pings_walks_the_route():
    pings = [(53.3000 + 0.0036 * i, -6.2000) for i in range(5)]
    assert matched_max_seq(STOPS, pings, RADIUS) == 5


def test_off_route_ping_does_not_poison_good_pings():
    pings = [(53.3072, -6.2000), (53.9000, -6.9000)]  # at stop 3 + garbage
    assert matched_max_seq(STOPS, pings, RADIUS) == 3


def test_empty_inputs_give_none():
    assert matched_max_seq([], [(53.3, -6.2)], RADIUS) is None
    assert matched_max_seq(STOPS, [], RADIUS) is None
