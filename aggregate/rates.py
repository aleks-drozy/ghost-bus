"""Wilson score intervals for the two published rates.

Pure functions: no database, no config, stdlib `math` only. Wilson rather than
the normal approximation because observed rates sit near zero on small samples,
where the naive interval produces negative bounds and a zero-width interval at
0 successes (design decision D2).
"""
from __future__ import annotations

import math


def wilson_interval(successes: int, trials: int, z: float = 1.96
                    ) -> tuple[float, float] | None:
    """95% Wilson score interval by default. None when there is nothing to divide by.

    Returns (lo, hi), clamped to [0.0, 1.0]. `trials <= 0` returns None rather
    than 0.0: an undefined rate is never reported as a real one.
    """
    if trials <= 0:
        return None
    p = successes / trials
    denom = 1.0 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom

    lo = max(0.0, centre - margin)
    hi = min(1.0, centre + margin)

    # Handle floating point precision for exact boundaries
    if successes == 0:
        lo = 0.0
    if successes == trials:
        hi = 1.0

    return (lo, hi)


def rate_with_interval(successes: int, trials: int
                       ) -> tuple[float, float, float] | None:
    """(rate, lo, hi) for one rate, or None when trials <= 0."""
    interval = wilson_interval(successes, trials)
    if interval is None:
        return None
    lo, hi = interval
    return (successes / trials, lo, hi)
