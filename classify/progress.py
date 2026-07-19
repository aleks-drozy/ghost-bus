"""Geographic progress: match vehicle GPS pings to a trip's scheduled stops.

Pure functions, no DB - the classifier queries SQLite and passes plain tuples.
A ping credits its NEAREST scheduled stop, and only if that stop lies within
radius_m; anything further contributes nothing (an off-route or glitched GPS
fix must never fabricate progress). Equidistant ties credit the lower
stop_sequence - progress is never over-credited.
"""
from __future__ import annotations

import math

_EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def matched_max_seq(stops: list[tuple[int, float, float]],
                    pings: list[tuple[float, float]],
                    radius_m: float) -> int | None:
    """Return the highest stop_sequence credited by any ping, or None.

    stops: (stop_sequence, lat, lon) for the trip's scheduled stops.
    pings: (lat, lon) vehicle positions observed during the trip window.

    Rules (each one is an honesty guarantee - see module docstring):
      1. A ping credits the stop NEAREST to it (by haversine_m), and only if
         that nearest stop is within radius_m. A ping whose nearest stop is
         farther contributes nothing at all.
      2. If two stops are exactly equidistant from a ping, credit the LOWER
         stop_sequence (never over-credit progress).
      3. The result is the maximum credited stop_sequence across all pings;
         None if no ping credited any stop (or either input is empty).
    """
    best: int | None = None
    for plat, plon in pings:
        near_seq: int | None = None
        near_d: float | None = None
        for seq, slat, slon in stops:
            d = haversine_m(plat, plon, slat, slon)
            if near_d is None or d < near_d or (d == near_d and seq < near_seq):
                near_seq, near_d = seq, d
        if near_d is not None and near_d <= radius_m:
            if best is None or near_seq > best:
                best = near_seq
    return best
