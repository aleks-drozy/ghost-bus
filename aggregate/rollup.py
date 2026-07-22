"""Roll trip outcomes up to route/day and route/local-hour tables."""
from __future__ import annotations

import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo

from aggregate.rates import rate_with_interval

_CLASSES = ("EXCLUDED", "CANCELLED", "COMPLETED", "VANISHED", "UNTRACKED",
            "EXCLUDED_FEED")

# The two published rates, each with its Wilson bounds. VANISHED and UNTRACKED
# are different claims about the world and are never summed (design decision
# D1): VANISHED is direct evidence a trip did not complete, UNTRACKED means we
# could not see it, which is also what a telematics failure looks like.
_RATED = ("vanished", "untracked")
RATE_KEYS = ("vanished_rate", "vanished_lo", "vanished_hi",
             "untracked_rate", "untracked_lo", "untracked_hi")


def _rates(counts: dict) -> dict:
    """Both rates over the same denominator: scheduled - excluded - excluded_feed.

    Tracker downtime (EXCLUDED) and feed degradation (EXCLUDED_FEED,
    amendment G3) never count against the operator - the denominator is the
    trips we could actually judge. When it is <= 0 every rate field is None -
    all six together, never a mix - because an undefined rate must not be
    reported as 0.0.
    """
    denom = counts["scheduled"] - counts["excluded"] - counts["excluded_feed"]
    out: dict = {}
    for kind in _RATED:
        result = rate_with_interval(counts[kind], denom)
        if result is None:
            out[f"{kind}_rate"] = None
            out[f"{kind}_lo"] = None
            out[f"{kind}_hi"] = None
        else:
            out[f"{kind}_rate"], out[f"{kind}_lo"], out[f"{kind}_hi"] = result
    return out


def _rollup(rows, key_fn):
    table: dict[tuple, dict] = {}
    for row in rows:
        key = key_fn(row)
        entry = table.setdefault(key, {c.lower(): 0 for c in _CLASSES} | {"scheduled": 0})
        entry["scheduled"] += 1
        entry[row["outcome"].lower()] += 1
    out = []
    for key, counts in sorted(table.items()):
        counts.update(_rates(counts))
        out.append(dict(zip(("route_id",) + (("service_date",) if len(key) == 2 and isinstance(key[1], str) else ("local_hour",)), key)) | counts)
    return out


def _fetch(db: sqlite3.Connection):
    cur = db.execute("SELECT trip_id, service_date, route_id, start_utc, outcome FROM trip_outcomes")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def route_day_rollup(db: sqlite3.Connection) -> list[dict]:
    return _rollup(_fetch(db), lambda r: (r["route_id"], r["service_date"]))


def route_hour_rollup(db: sqlite3.Connection, tz: str = "Europe/Dublin") -> list[dict]:
    zone = ZoneInfo(tz)

    def key(r):
        local = dt.datetime.fromisoformat(r["start_utc"]).astimezone(zone)
        return (r["route_id"], local.hour)

    return _rollup(_fetch(db), key)
