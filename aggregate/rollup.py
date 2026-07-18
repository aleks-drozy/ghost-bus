"""Roll trip outcomes up to route/day and route/local-hour tables."""
from __future__ import annotations

import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo

_CLASSES = ("EXCLUDED", "CANCELLED", "COMPLETED", "VANISHED", "UNTRACKED")


def _ghost_rate(counts: dict) -> float | None:
    denom = counts["scheduled"] - counts["excluded"]
    if denom <= 0:
        return None
    return (counts["untracked"] + counts["vanished"]) / denom


def _rollup(rows, key_fn):
    table: dict[tuple, dict] = {}
    for row in rows:
        key = key_fn(row)
        entry = table.setdefault(key, {c.lower(): 0 for c in _CLASSES} | {"scheduled": 0})
        entry["scheduled"] += 1
        entry[row["outcome"].lower()] += 1
    out = []
    for key, counts in sorted(table.items()):
        counts["ghost_rate"] = _ghost_rate(counts)
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
