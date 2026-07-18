"""Publish gate: the site never ships numbers these checks didn't pass."""
from __future__ import annotations

import sqlite3
import sys

from aggregate.rollup import route_day_rollup
from classify.outcomes import OUTCOMES


def check_conservation(db: sqlite3.Connection) -> dict:
    bad = []
    for r in route_day_rollup(db):
        parts = r["excluded"] + r["cancelled"] + r["completed"] + r["vanished"] + r["untracked"]
        if parts != r["scheduled"]:
            bad.append(r)
    return {"check": "conservation", "passed": not bad, "violations": bad}


def check_rates_bounded(db: sqlite3.Connection) -> dict:
    bad = [r for r in route_day_rollup(db)
           if r["ghost_rate"] is not None and not 0.0 <= r["ghost_rate"] <= 1.0]
    return {"check": "rates_bounded", "passed": not bad, "violations": bad}


def check_outcomes_valid(db: sqlite3.Connection) -> dict:
    marks = ",".join("?" * len(OUTCOMES))
    bad = db.execute(
        f"SELECT trip_id, outcome FROM trip_outcomes WHERE outcome NOT IN ({marks})",
        OUTCOMES).fetchall()
    return {"check": "outcomes_valid", "passed": not bad, "violations": bad}


def main() -> int:
    db = sqlite3.connect(sys.argv[1] if len(sys.argv) > 1 else "state/ghostbus.db")
    # Outcomes validity gates the other two checks: conservation and rates_bounded
    # both key into per-outcome dict slots (see aggregate/rollup.py), so an
    # unrecognized outcome string would KeyError there instead of failing cleanly.
    outcomes_result = check_outcomes_valid(db)
    if not outcomes_result["passed"]:
        print("FAIL", outcomes_result["check"])
        print("SKIP conservation (invalid outcomes present)")
        print("SKIP rates_bounded (invalid outcomes present)")
        return 1
    results = [check_conservation(db), check_rates_bounded(db), outcomes_result]
    for r in results:
        print(("PASS" if r["passed"] else "FAIL"), r["check"])
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
