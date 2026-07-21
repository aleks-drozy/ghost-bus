# Publisher & Public Scoreboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a route-reliability scoreboard and an open dataset, built so that the published numbers cannot drift from the published data, ranked so that routes are only called worse than one another when the sample supports it, and gated so that nothing route-level ships before the agreed baseline exists.

**Architecture:** The VM runs `publish/dataset.py` nightly behind the publish gate, turning SQLite into `data/daily/*.csv`, `data/uptime/*.csv` and `data/manifest.json`, and pushes those files — and only those — to a separate dataset repository. GitHub Actions checks that dataset out beside the code, runs the full test suite, then builds static HTML from the published CSVs with `publish/site.py` and deploys it to GitHub Pages. The site never opens the database, so a figure on a page cannot differ from the figure in the file you can download.

**Tech Stack:** Python 3.12, stdlib only — `sqlite3`, `csv`, `json`, `math`, `string.Template`, `html.escape`, `zoneinfo`, `argparse`. SQLite on one 1 GB Oracle VM, systemd timer, bash, GitHub Actions, GitHub Pages. No new dependencies, no JavaScript, no external assets.

## Global Constraints

- **D1 — two rates, never summed.** VANISHED and UNTRACKED are computed, published, and displayed separately, and **no code path sums them**. A test pins this.
- **D2 — Wilson score intervals, rank by the lower bound.** Every published rate carries a 95% Wilson score interval. **The leaderboard ranks by the lower bound of the VANISHED rate specifically** — descending, worst first. The untracked rate is displayed with its own interval in its own column and never contributes to rank position.
- **D3 — the site is built from the published dataset, never from the database.** The VM publishes CSVs; GitHub Actions builds HTML *from those CSVs*.
- **D4 — split trust: the VM publishes data, CI publishes the site.** The VM's credential cannot publish arbitrary HTML.
- **D5 — stdlib templating, escaping test-pinned.** `string.Template` plus explicit `html.escape()`. Route names come from GTFS — external input — so escaping is a security requirement, not a nicety.
- **D6 — two publication gates, enforced in code.** `>=30` **judgeable trips (`scheduled − excluded`)** in the window before a route is ranked — the same denominator as the rates the gate guards, never `scheduled`; `>=14` complete service days before *any* route-level number is published, in the dataset or on the site. Below-threshold routes still appear, under "not enough data yet", with their counts visible.
- **D7 — complete service days only.** A service day is published only when `service_date < today` in Europe/Dublin.
- **D8 — rolling 28-day leaderboard window.** Full history stays in the per-day CSVs.
- Denominator for both rates is `scheduled - excluded` (tracker downtime never counts against the operator), consistent with the existing rollup.
- `rate_with_interval` returns `None` when `trials == 0` — an undefined rate is never reported as 0.0. Bounds are clamped to `[0.0, 1.0]`.
- `trials == 0` for a rate is reported as "—", never as 0.0.
- Uptime CSVs are deliberately exempt from the 14-day gate and publish from day one: our own downtime is not a claim about any operator.
- A day with no data is rendered as a visible gap; never interpolated.
- Every externally-sourced string is `html.escape()`d before templating.
- No JavaScript, no analytics, no cookies, no external fonts or CDN assets — the site makes no third-party requests at all.
- Full suite (`python -m pytest -q`) green before every commit. Baseline is **150 passing** on `main`.

---

### Task 1: `aggregate/rates.py` — Wilson score interval and rate-with-interval

**Files:**
- Create: `C:\Users\Alex\Projects\ghost-bus\aggregate\rates.py`
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_rates.py` (create)

**Interfaces:**
- Consumes: nothing (pure stdlib `math`, no DB, no config)
- Produces:
  - `aggregate.rates.wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float] | None`
  - `aggregate.rates.rate_with_interval(successes: int, trials: int) -> tuple[float, float, float] | None` returning `(rate, lo, hi)`

Both return `None` when `trials <= 0`. Bounds are clamped to `[0.0, 1.0]`. Task 2 (`aggregate/rollup.py`) calls `rate_with_interval` twice per rollup row; Task 12 calls it once per route per rate.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_rates.py` with exactly this content:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `C:\Users\Alex\Projects\ghost-bus`):
```
python -m pytest tests/test_rates.py -q
```
Expected: FAIL — collection error, `ModuleNotFoundError: No module named 'aggregate.rates'`, reported as `ERROR tests/test_rates.py` with `1 error` in the summary.

- [ ] **Step 3: Write minimal implementation**

Create `C:\Users\Alex\Projects\ghost-bus\aggregate\rates.py` with exactly this content:

```python
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
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def rate_with_interval(successes: int, trials: int
                       ) -> tuple[float, float, float] | None:
    """(rate, lo, hi) for one rate, or None when trials <= 0."""
    interval = wilson_interval(successes, trials)
    if interval is None:
        return None
    lo, hi = interval
    return (successes / trials, lo, hi)
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```
python -m pytest tests/test_rates.py -q
```
Expected: PASS — `11 passed`.

Then run the full suite:
```
python -m pytest -q
```
Expected: PASS — `161 passed` (150 pre-existing + 11 new), 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```
cd C:\Users\Alex\Projects\ghost-bus
git add aggregate/rates.py tests/test_rates.py
git commit -m "feat(aggregate): Wilson score intervals for published rates

Adds wilson_interval() and rate_with_interval(), stdlib-only, returning
None when trials <= 0 and clamping bounds to [0,1]. Wilson rather than the
normal approximation because rates sit near zero on small samples (D2).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Split `ghost_rate` into vanished and untracked rates across rollup and the publish gate

**Files:**
- Modify: `C:\Users\Alex\Projects\ghost-bus\aggregate\rollup.py:1-29` (replace `_ghost_rate` at lines 11-15; replace the `counts["ghost_rate"]` assignment at line 27; add an import)
- Modify: `C:\Users\Alex\Projects\ghost-bus\run_checks.py:7` (import) and `run_checks.py:20-23` (`check_rates_bounded`)
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_rollup.py:32-41` (rewrite the two tests that assert on `ghost_rate`)
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_checks.py` (append new tests after line 66)

**THIS TASK IS ATOMIC — DO NOT SPLIT IT ACROSS COMMITS.** `run_checks.check_rates_bounded` reads `r["ghost_rate"]` today (`run_checks.py:22`). The moment `aggregate/rollup.py` stops emitting that key, `check_rates_bounded` raises `KeyError: 'ghost_rate'` and `tests/test_checks.py::test_all_checks_pass_on_good_db` goes red. `aggregate/rollup.py` and `run_checks.py` must change together, in one commit, with the suite green at the end. A `grep` for `ghost_rate` confirms these two are the only live consumers (the other hits are in `docs/`, which is historical record and must not be edited).

**Two existing tests WILL break and this task owns fixing them** — `tests/test_rollup.py::test_route_day_counts_and_ghost_rate` (line 36: `assert r1["ghost_rate"] == pytest.approx((1 + 1) / (5 - 1))`) and `tests/test_rollup.py::test_all_excluded_route_has_null_rate` (line 41: `... and r2["ghost_rate"] is None`). They are replaced, not deleted: the same facts are re-asserted against the split rates. Nothing was ever published under the old combined definition, so no public claim is being revised.

**Interfaces:**
- Consumes: `aggregate.rates.rate_with_interval(successes, trials) -> tuple[float, float, float] | None` (Task 1)
- Produces:
  - `aggregate.rollup.route_day_rollup(db) -> list[dict]` — unchanged name and shape, one dict per `(route_id, service_date)` with keys `route_id, service_date, scheduled, excluded, cancelled, completed, vanished, untracked, vanished_rate, vanished_lo, vanished_hi, untracked_rate, untracked_lo, untracked_hi`. No `ghost_rate` key exists any more.
  - `aggregate.rollup.route_hour_rollup(db, tz="Europe/Dublin") -> list[dict]` — same keys with `local_hour` in place of `service_date`.
  - All six rate keys are `None` together when `scheduled - excluded <= 0`; never partially populated.
  - `run_checks.check_rates_bounded(db) -> dict` — same `{"check", "passed", "violations"}` shape, now validating both rates.
  - `aggregate.rollup.RATE_KEYS` — the six-name tuple, imported by `run_checks.py` and by Task 3.

Denominator for both rates is `scheduled - excluded` (tracker downtime never counts against the operator). The two rates are never added anywhere.

- [ ] **Step 1: Write the failing test**

First, replace lines 32-41 of `C:\Users\Alex\Projects\ghost-bus\tests\test_rollup.py`. The two functions currently there are:

```python
def test_route_day_counts_and_ghost_rate(db):
    rollup = route_day_rollup(db)
    r1 = next(r for r in rollup if r["route_id"] == "R1")
    assert r1["scheduled"] == 5 and r1["excluded"] == 1
    assert r1["ghost_rate"] == pytest.approx((1 + 1) / (5 - 1))


def test_all_excluded_route_has_null_rate(db):
    r2 = next(r for r in route_day_rollup(db) if r["route_id"] == "R2")
    assert r2["scheduled"] == 1 and r2["excluded"] == 1 and r2["ghost_rate"] is None
```

Replace both with exactly:

```python
def test_route_day_counts_and_split_rates(db):
    rollup = route_day_rollup(db)
    r1 = next(r for r in rollup if r["route_id"] == "R1")
    assert r1["scheduled"] == 5 and r1["excluded"] == 1
    assert r1["vanished"] == 1 and r1["untracked"] == 1
    # Denominator for both rates is scheduled - excluded = 4. The two rates are
    # reported separately and are never summed (design decision D1); the old
    # combined ghost_rate of 2/4 is gone and must not reappear.
    assert r1["vanished_rate"] == pytest.approx(1 / 4)
    assert r1["untracked_rate"] == pytest.approx(1 / 4)
    # Wilson 95% interval for 1/4, hand-computed - see tests/test_rates.py.
    assert r1["vanished_lo"] == pytest.approx(0.045586062644636216, rel=1e-12)
    assert r1["vanished_hi"] == pytest.approx(0.6993639475573634, rel=1e-12)
    assert r1["untracked_lo"] == pytest.approx(0.045586062644636216, rel=1e-12)
    assert r1["untracked_hi"] == pytest.approx(0.6993639475573634, rel=1e-12)
    assert "ghost_rate" not in r1


def test_all_excluded_route_has_null_rates(db):
    r2 = next(r for r in route_day_rollup(db) if r["route_id"] == "R2")
    assert r2["scheduled"] == 1 and r2["excluded"] == 1
    # Denominator is 0: every rate field is None, never 0.0, and never a mix.
    for key in ("vanished_rate", "vanished_lo", "vanished_hi",
                "untracked_rate", "untracked_lo", "untracked_hi"):
        assert r2[key] is None, key
    assert "ghost_rate" not in r2


def test_hour_rollup_carries_the_same_rate_keys(db):
    hours = {(r["route_id"], r["local_hour"]): r for r in route_hour_rollup(db)}
    row = hours[("R1", 8)]
    # 08:00 UTC VANISHED + 08:30 UTC EXCLUDED -> scheduled 2, excluded 1, denom 1.
    assert row["scheduled"] == 2 and row["excluded"] == 1
    assert row["vanished_rate"] == pytest.approx(1.0)
    assert row["untracked_rate"] == pytest.approx(0.0)
    assert "ghost_rate" not in row
```

Second, append these tests to the end of `C:\Users\Alex\Projects\ghost-bus\tests\test_checks.py` (after line 66):

```python


def test_rates_bounded_passes_on_real_rollup_rows():
    db = make_db(GOOD)
    result = check_rates_bounded(db)
    assert result["passed"] and result["violations"] == []


def test_rates_bounded_flags_out_of_range_vanished_rate(monkeypatch):
    import run_checks

    bad = {"route_id": "R1", "service_date": "2026-03-23", "scheduled": 10,
           "excluded": 0, "cancelled": 0, "completed": 8, "vanished": 1, "untracked": 1,
           "vanished_rate": 1.5, "vanished_lo": 0.0, "vanished_hi": 1.0,
           "untracked_rate": 0.1, "untracked_lo": 0.0, "untracked_hi": 0.4}
    monkeypatch.setattr(run_checks, "route_day_rollup", lambda db: [bad])
    result = run_checks.check_rates_bounded(None)
    assert not result["passed"] and result["violations"] == [bad]


def test_rates_bounded_flags_out_of_range_untracked_bound(monkeypatch):
    import run_checks

    bad = {"route_id": "R1", "service_date": "2026-03-23", "scheduled": 10,
           "excluded": 0, "cancelled": 0, "completed": 8, "vanished": 1, "untracked": 1,
           "vanished_rate": 0.1, "vanished_lo": 0.0, "vanished_hi": 0.4,
           "untracked_rate": 0.1, "untracked_lo": -0.2, "untracked_hi": 0.4}
    monkeypatch.setattr(run_checks, "route_day_rollup", lambda db: [bad])
    result = run_checks.check_rates_bounded(None)
    assert not result["passed"] and result["violations"] == [bad]


def test_rates_bounded_flags_point_estimate_outside_its_interval(monkeypatch):
    import run_checks

    bad = {"route_id": "R1", "service_date": "2026-03-23", "scheduled": 10,
           "excluded": 0, "cancelled": 0, "completed": 8, "vanished": 1, "untracked": 1,
           "vanished_rate": 0.9, "vanished_lo": 0.0, "vanished_hi": 0.4,
           "untracked_rate": 0.1, "untracked_lo": 0.0, "untracked_hi": 0.4}
    monkeypatch.setattr(run_checks, "route_day_rollup", lambda db: [bad])
    result = run_checks.check_rates_bounded(None)
    assert not result["passed"] and result["violations"] == [bad]


def test_rates_bounded_flags_partially_defined_rates(monkeypatch):
    import run_checks

    # All six rate fields share one denominator, so they are None together or
    # populated together. A half-populated row means the rollup is broken.
    bad = {"route_id": "R1", "service_date": "2026-03-23", "scheduled": 10,
           "excluded": 0, "cancelled": 0, "completed": 8, "vanished": 1, "untracked": 1,
           "vanished_rate": 0.1, "vanished_lo": 0.0, "vanished_hi": 0.4,
           "untracked_rate": None, "untracked_lo": None, "untracked_hi": None}
    monkeypatch.setattr(run_checks, "route_day_rollup", lambda db: [bad])
    result = run_checks.check_rates_bounded(None)
    assert not result["passed"] and result["violations"] == [bad]


def test_rates_bounded_passes_when_all_rates_are_null(monkeypatch):
    import run_checks

    row = {"route_id": "R2", "service_date": "2026-03-23", "scheduled": 1,
           "excluded": 1, "cancelled": 0, "completed": 0, "vanished": 0, "untracked": 0,
           "vanished_rate": None, "vanished_lo": None, "vanished_hi": None,
           "untracked_rate": None, "untracked_lo": None, "untracked_hi": None}
    monkeypatch.setattr(run_checks, "route_day_rollup", lambda db: [row])
    assert run_checks.check_rates_bounded(None)["passed"]
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `C:\Users\Alex\Projects\ghost-bus`):
```
python -m pytest tests/test_rollup.py tests/test_checks.py -q
```
Expected: FAIL — `8 failed`. `tests/test_rollup.py::test_route_day_counts_and_split_rates` fails with `KeyError: 'vanished_rate'` raised at `assert r1["vanished_rate"] == pytest.approx(1 / 4)`; `test_all_excluded_route_has_null_rates` and `test_hour_rollup_carries_the_same_rate_keys` fail with `KeyError: 'vanished_rate'`; and the five new `tests/test_checks.py` monkeypatch tests fail with `KeyError: 'ghost_rate'` (the current `check_rates_bounded` reads a key the fabricated rows do not have). `test_rates_bounded_passes_on_real_rollup_rows` passes already.

- [ ] **Step 3: Write minimal implementation**

Replace the entire contents of `C:\Users\Alex\Projects\ghost-bus\aggregate\rollup.py` with:

```python
"""Roll trip outcomes up to route/day and route/local-hour tables."""
from __future__ import annotations

import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo

from aggregate.rates import rate_with_interval

_CLASSES = ("EXCLUDED", "CANCELLED", "COMPLETED", "VANISHED", "UNTRACKED")

# The two published rates, each with its Wilson bounds. VANISHED and UNTRACKED
# are different claims about the world and are never summed (design decision
# D1): VANISHED is direct evidence a trip did not complete, UNTRACKED means we
# could not see it, which is also what a telematics failure looks like.
_RATED = ("vanished", "untracked")
RATE_KEYS = ("vanished_rate", "vanished_lo", "vanished_hi",
             "untracked_rate", "untracked_lo", "untracked_hi")


def _rates(counts: dict) -> dict:
    """Both rates over the same denominator: scheduled - excluded.

    Tracker downtime (EXCLUDED) never counts against the operator. When the
    denominator is <= 0 every rate field is None - all six together, never a
    mix - because an undefined rate must not be reported as 0.0.
    """
    denom = counts["scheduled"] - counts["excluded"]
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
```

Then change the import on `C:\Users\Alex\Projects\ghost-bus\run_checks.py:7` from:

```python
from aggregate.rollup import route_day_rollup
```

to:

```python
from aggregate.rollup import RATE_KEYS, route_day_rollup
```

And replace lines 20-23 of `C:\Users\Alex\Projects\ghost-bus\run_checks.py`. The current function is:

```python
def check_rates_bounded(db: sqlite3.Connection) -> dict:
    bad = [r for r in route_day_rollup(db)
           if r["ghost_rate"] is not None and not 0.0 <= r["ghost_rate"] <= 1.0]
    return {"check": "rates_bounded", "passed": not bad, "violations": bad}
```

Replace it with exactly:

```python
def check_rates_bounded(db: sqlite3.Connection) -> dict:
    """Both published rates in [0,1], each point estimate inside its own interval.

    The vanished and untracked rates are validated independently and are never
    added together (design decision D1). All six fields share one denominator,
    so they are either all None or all populated; a mix means the rollup broke.
    """
    bad = []
    for r in route_day_rollup(db):
        values = [r[key] for key in RATE_KEYS]
        present = [v for v in values if v is not None]
        if not present:
            continue
        if len(present) != len(values):
            bad.append(r)
            continue
        if any(not 0.0 <= v <= 1.0 for v in present):
            bad.append(r)
            continue
        if not (r["vanished_lo"] <= r["vanished_rate"] <= r["vanished_hi"]
                and r["untracked_lo"] <= r["untracked_rate"] <= r["untracked_hi"]):
            bad.append(r)
    return {"check": "rates_bounded", "passed": not bad, "violations": bad}
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```
python -m pytest tests/test_rollup.py tests/test_checks.py -q
```
Expected: PASS — `14 passed` (5 in `test_rollup.py`, 9 in `test_checks.py`).

Then the full suite:
```
python -m pytest -q
```
Expected: PASS — `168 passed`, 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```
cd C:\Users\Alex\Projects\ghost-bus
git add aggregate/rollup.py run_checks.py tests/test_rollup.py tests/test_checks.py
git commit -m "feat(aggregate)!: publish vanished and untracked rates separately

Replaces the combined ghost_rate with vanished_rate/untracked_rate, each
carrying its Wilson 95% bounds, over the same scheduled-excluded denominator.
The two rates are different claims - VANISHED is evidence a trip did not
complete, UNTRACKED is also what a telematics failure looks like - so summing
them would contradict the methodology page (D1). No code path sums them.

check_rates_bounded now validates both rates, their bounds, and that each
point estimate sits inside its own interval. It had to change in the same
commit: it read r['ghost_rate'], which no longer exists.

The two rollup tests that asserted on ghost_rate are rewritten against the
split rates; nothing was ever published under the old definition.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Pin the never-summed invariant

**Files:**
- Modify: `C:\Users\Alex\Projects\ghost-bus\tests\test_rates.py` (append; created by Task 1)

This task adds no production code. It is a regression pin for design decision D1 — the spec requires a test asserting that no published row exposes a field equal to `vanished + untracked` and that no such combined key exists. It is separate from Task 2 because it guards every future producer of rollup rows (`publish/dataset.py` in Task 5 and `publish/site.py` in Tasks 12-14), not just the change Task 2 made.

**Interfaces:**
- Consumes: `aggregate.rollup.route_day_rollup(db) -> list[dict]` and `aggregate.rollup.RATE_KEYS` (Task 2)
- Produces: no new symbols. `tests/test_rates.py::test_no_published_field_equals_the_sum_of_the_two_rates` becomes the invariant every later task must keep green.

- [ ] **Step 1: Write the failing test**

Append to the end of `C:\Users\Alex\Projects\ghost-bus\tests\test_rates.py`:

```python


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
        # R1: scheduled 5, excluded 1 -> denominator 4, one VANISHED, one
        # UNTRACKED. Each rate is 0.25; the forbidden sum is 0.50, a value that
        # matches no other field on the row, so a leak is unambiguous.
        ("a", "2026-03-23", "R1", "2026-03-23T07:00:00+00:00", "COMPLETED"),
        ("b", "2026-03-23", "R1", "2026-03-23T07:30:00+00:00", "UNTRACKED"),
        ("c", "2026-03-23", "R1", "2026-03-23T08:00:00+00:00", "VANISHED"),
        ("d", "2026-03-23", "R1", "2026-03-23T08:30:00+00:00", "EXCLUDED"),
        ("e", "2026-03-23", "R1", "2026-03-23T09:00:00+00:00", "CANCELLED"),
        # R2: every trip excluded -> denominator 0, all rates None.
        ("f", "2026-03-23", "R2", "2026-03-23T09:00:00+00:00", "EXCLUDED"),
        # R3: scheduled 4, three UNTRACKED and one VANISHED -> rates 0.75 and
        # 0.25, forbidden sum 1.0, which also catches a clamped-to-1.0 leak.
        ("g", "2026-03-24", "R3", "2026-03-24T07:00:00+00:00", "UNTRACKED"),
        ("h", "2026-03-24", "R3", "2026-03-24T07:30:00+00:00", "UNTRACKED"),
        ("i", "2026-03-24", "R3", "2026-03-24T08:00:00+00:00", "UNTRACKED"),
        ("j", "2026-03-24", "R3", "2026-03-24T08:30:00+00:00", "VANISHED"),
    ]
    conn.executemany("INSERT INTO trip_outcomes VALUES (?,?,?,?,?)", rows)
    conn.commit()
    return conn


def test_no_combined_rate_key_exists():
    for row in route_day_rollup(_invariant_db()):
        for key in row:
            assert "ghost" not in key, f"combined-rate key reappeared: {key}"
            assert "combined" not in key, f"combined-rate key reappeared: {key}"
        assert "ghost_rate" not in row
        assert "combined_rate" not in row
        assert "failure_rate" not in row


def test_published_rate_keys_are_exactly_the_six_split_fields():
    assert RATE_KEYS == ("vanished_rate", "vanished_lo", "vanished_hi",
                         "untracked_rate", "untracked_lo", "untracked_hi")
    for row in route_day_rollup(_invariant_db()):
        rate_like = {k for k in row if k.endswith(("_rate", "_lo", "_hi"))}
        assert rate_like == set(RATE_KEYS), rate_like


def test_no_published_field_equals_the_sum_of_the_two_rates():
    for row in route_day_rollup(_invariant_db()):
        denom = row["scheduled"] - row["excluded"]
        if denom <= 0:
            for key in RATE_KEYS:
                assert row[key] is None, key
            continue
        combined = (row["vanished"] + row["untracked"]) / denom
        if combined == 0.0:
            # Both rates are legitimately 0.0 here, so equality proves nothing.
            continue
        assert row["vanished_rate"] + row["untracked_rate"] == pytest.approx(combined)
        for key, value in row.items():
            if isinstance(value, float):
                assert value != pytest.approx(combined), (
                    f"{key} on route {row['route_id']} equals vanished+untracked "
                    f"({combined}); the two rates must never be summed (D1)")


def test_the_two_rates_are_reported_independently():
    rows = {r["route_id"]: r for r in route_day_rollup(_invariant_db())}
    r3 = rows["R3"]
    # Different numerators over the same denominator: proof they are not one
    # number wearing two names.
    assert r3["vanished_rate"] == pytest.approx(1 / 4)
    assert r3["untracked_rate"] == pytest.approx(3 / 4)
    assert r3["vanished_lo"] != pytest.approx(r3["untracked_lo"])
    assert r3["vanished_hi"] != pytest.approx(r3["untracked_hi"])
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```
python -m pytest tests/test_rates.py -q
```

This is a regression pin, not a red-then-green cycle: Task 2 already made the invariant true, so the expected result here is **PASS — `15 passed`**.

To prove the pin actually bites before trusting it, temporarily add this line to `C:\Users\Alex\Projects\ghost-bus\aggregate\rollup.py` inside `_rates`, immediately before `return out`:

```python
    out["ghost_rate"] = (counts["vanished"] + counts["untracked"]) / denom if denom > 0 else None
```

Re-run:
```
python -m pytest tests/test_rates.py -q
```
Expected: FAIL — `3 failed`: `test_no_combined_rate_key_exists` with `AssertionError: combined-rate key reappeared: ghost_rate`, `test_published_rate_keys_are_exactly_the_six_split_fields` with an `AssertionError` showing `ghost_rate` in the rate-like set, and `test_no_published_field_equals_the_sum_of_the_two_rates` with `AssertionError: ghost_rate on route R1 equals vanished+untracked (0.5); the two rates must never be summed (D1)`.

- [ ] **Step 3: Write minimal implementation**

No production change is required — Task 2 supplied it. Remove the temporary line added in Step 2 so `aggregate/rollup.py` is byte-identical to the version committed in Task 2:

```
cd C:\Users\Alex\Projects\ghost-bus
git checkout -- aggregate/rollup.py
git diff --stat aggregate/rollup.py
```
Expected: `git diff --stat` prints nothing (no modifications to `aggregate/rollup.py`).

If Step 2 showed a genuine failure with `aggregate/rollup.py` unmodified, the implementation is wrong, not the test: `_rates` must write exactly the six keys in `RATE_KEYS` and nothing else, and no caller may add a summed field. Fix `_rates` in `aggregate/rollup.py` — do not weaken this test.

- [ ] **Step 4: Run test to verify it passes**

Run:
```
python -m pytest tests/test_rates.py -q
```
Expected: PASS — `15 passed`.

Then:
```
python -m pytest -q
```
Expected: PASS — `172 passed`, 0 failed, 0 errors.

And confirm no unintended source change is staged:
```
git status --porcelain
```
Expected: exactly one line, ` M tests/test_rates.py`.

- [ ] **Step 5: Commit**

```
cd C:\Users\Alex\Projects\ghost-bus
git add tests/test_rates.py
git commit -m "test(aggregate): pin the never-summed invariant for the two rates

Asserts no rollup row carries a key or a value equal to vanished+untracked,
that the rate-like keys are exactly the six split fields, and that the two
rates move independently. Guards D1 against every future producer of these
rows - publish/dataset.py and publish/site.py included.

Verified the pin bites by temporarily reintroducing a combined field: three
tests fail, naming the offending key.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Publisher skeleton, uptime CSVs, and un-ignoring the dataset paths

**Files:**
- Create: `C:\Users\Alex\Projects\ghost-bus\publish\__init__.py`
- Create: `C:\Users\Alex\Projects\ghost-bus\publish\dataset.py`
- Modify: `C:\Users\Alex\Projects\ghost-bus\.gitignore:6` (replace the line `data/` with `data/probe/`)
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\dataset_fixture.py` (create; shared by Tasks 5-6 and 8-9)
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_dataset_uptime.py`

**Interfaces:**
- Consumes: `classify/store.py` table `heartbeats(ts_utc TEXT PRIMARY KEY, ok INTEGER)`, where `ts_utc` is an aware-UTC `datetime.isoformat()` string written by `ingest/poller.py`, e.g. `2026-03-23T00:00:00.100000+00:00`.
- Produces, relied on by Tasks 5-6 and 8-9:
  - `SCHEMA_VERSION: int`, `BASELINE_REQUIRED_DAYS: int`, `LOCAL_TZ: str`, `UTC`, `expected_minutes(day, tz=LOCAL_TZ) -> int`
  - `UPTIME_COLUMNS: tuple[str, ...]`
  - `_write_csv(path: pathlib.Path, columns, rows) -> None`
  - `local_today(tz: str = LOCAL_TZ) -> datetime.date`
  - `day_bounds_utc(day: datetime.date, tz: str = LOCAL_TZ) -> tuple[datetime.datetime, datetime.datetime]`
  - `uptime_days(db, today: datetime.date) -> list[datetime.date]`
  - `uptime_row(db, day: datetime.date) -> dict`
  - `write_uptime_csvs(db, data_dir, days) -> list[pathlib.Path]`
- Produces (test side): `tests/dataset_fixture.py` exposing `SERVICE_DATE`, `GTFS_HASH`, `GTFS_LOADED_AT`, `UNNAMED_ROUTE_ID`, `HEARTBEATS`, `OBSERVATIONS`, `GTFS_ROUTES`, `GTFS_AGENCY`, `outcome_rows(service_date)`, `consecutive_dates(n, start)`, `build_db(service_dates=(SERVICE_DATE,), heartbeats=None)`.

**Why the `.gitignore` edit belongs here.** `.gitignore:6` is the literal line `data/`, which was added for the `data/probe/*.pb` capture fixtures. Left as-is it also ignores every path this publisher writes: `git add -- data` would stage nothing, `git diff --cached --quiet` would exit 0, and the whole nightly pipeline would report "dataset unchanged" forever while publishing nothing. Narrowing it to `data/probe/` keeps the probe captures out of the repo and makes the dataset visible to git. A test pins it, because the failure mode is a silent success message.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\Alex\Projects\ghost-bus\tests\dataset_fixture.py`:

```python
"""Shared synthetic database for publish/dataset.py tests.

Offline and in-memory. Deliberately shaped to exercise every published edge:
a normally-populated route, a route whose denominator is zero (all EXCLUDED),
and a production-shaped route_id containing spaces that is absent from
gtfs_routes.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

# A Monday, six days before the 2026-03-29 DST change, so Europe/Dublin is
# UTC+0 that day and the golden files stay readable.
SERVICE_DATE = "2026-03-23"

# sha256 of the empty byte string: a fixed, obviously-synthetic 64-hex digest.
GTFS_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
GTFS_LOADED_AT = "2026-03-01T02:00:00+00:00"

_SCHEMA = """
CREATE TABLE trip_outcomes (
  trip_id TEXT, service_date TEXT, route_id TEXT, start_utc TEXT, outcome TEXT,
  PRIMARY KEY (trip_id, service_date));
CREATE TABLE heartbeats (ts_utc TEXT PRIMARY KEY, ok INTEGER);
CREATE TABLE observations (
  trip_id TEXT, service_date TEXT, ts_utc TEXT, kind TEXT, stop_sequence INTEGER,
  lat REAL, lon REAL, vehicle_ts TEXT);
CREATE TABLE gtfs_routes (route_id TEXT PRIMARY KEY, agency_id TEXT,
  route_short_name TEXT, route_long_name TEXT);
CREATE TABLE gtfs_agency (agency_id TEXT PRIMARY KEY, agency_name TEXT);
CREATE TABLE gtfs_meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Two ok polls land in the same minute bucket (00:00) - a retry storm must not
# inflate uptime. One failed poll is not an ok minute. Net: 2 ok minutes.
HEARTBEATS = [
    ("2026-03-23T00:00:00.100000+00:00", 1),
    ("2026-03-23T00:00:30.100000+00:00", 1),
    ("2026-03-23T00:01:00.100000+00:00", 1),
    ("2026-03-23T00:02:00.100000+00:00", 0),
]

OBSERVATIONS = [
    ("R1_00_2026-03-23", SERVICE_DATE, "2026-03-23T07:01:00+00:00", "position",
     1, 53.3000, -6.2000, None),
    ("R1_00_2026-03-23", SERVICE_DATE, "2026-03-23T07:15:00+00:00", "position",
     3, 53.3072, -6.2000, None),
    ("R1_03_2026-03-23", SERVICE_DATE, "2026-03-23T10:05:00+00:00", "update",
     2, None, None, None),
]

GTFS_ROUTES = [
    ("R1", "FVB", "1", "Fixtureville Main"),
    ("R2", "FVB", "2", "Fixtureville Orbital"),
]
GTFS_AGENCY = [("FVB", "Fixtureville Bus")]

# Absent from GTFS_ROUTES on purpose: production route ids look like this and
# must surface in manifest.unnamed_routes rather than being dropped.
UNNAMED_ROUTE_ID = "03C 120 e a"


def outcome_rows(service_date: str = SERVICE_DATE) -> list[tuple]:
    """R1: 10 scheduled / 2 excluded -> denominator 8, 1 vanished, 1 untracked.
    R2: 1 scheduled / 1 excluded -> denominator 0, both rates undefined.
    UNNAMED_ROUTE_ID: 2 scheduled, 1 completed, 1 vanished."""
    kinds = (["EXCLUDED"] * 2 + ["CANCELLED"] + ["COMPLETED"] * 5
             + ["VANISHED"] + ["UNTRACKED"])
    rows = [(f"R1_{i:02d}_{service_date}", service_date, "R1",
             f"{service_date}T{7 + i:02d}:00:00+00:00", kind)
            for i, kind in enumerate(kinds)]
    rows.append((f"R2_00_{service_date}", service_date, "R2",
                 f"{service_date}T09:00:00+00:00", "EXCLUDED"))
    rows.append((f"U_00_{service_date}", service_date, UNNAMED_ROUTE_ID,
                 f"{service_date}T10:00:00+00:00", "COMPLETED"))
    rows.append((f"U_01_{service_date}", service_date, UNNAMED_ROUTE_ID,
                 f"{service_date}T11:00:00+00:00", "VANISHED"))
    return rows


def consecutive_dates(n: int, start: str = "2026-03-02") -> list[str]:
    d0 = dt.date.fromisoformat(start)
    return [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n)]


def build_db(service_dates=(SERVICE_DATE,), heartbeats=None) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(_SCHEMA)
    for day in service_dates:
        db.executemany("INSERT INTO trip_outcomes VALUES (?,?,?,?,?)",
                       outcome_rows(day))
    db.executemany("INSERT INTO heartbeats VALUES (?,?)",
                   HEARTBEATS if heartbeats is None else heartbeats)
    db.executemany("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)", OBSERVATIONS)
    db.executemany("INSERT INTO gtfs_routes VALUES (?,?,?,?)", GTFS_ROUTES)
    db.executemany("INSERT INTO gtfs_agency VALUES (?,?)", GTFS_AGENCY)
    db.execute("INSERT INTO gtfs_meta VALUES ('gtfs_hash', ?)", (GTFS_HASH,))
    db.execute("INSERT INTO gtfs_meta VALUES ('gtfs_loaded_at', ?)", (GTFS_LOADED_AT,))
    db.commit()
    return db
```

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_dataset_uptime.py`:

```python
import datetime as dt
import shutil
import subprocess
from pathlib import Path

import pytest

from publish.dataset import (UPTIME_COLUMNS, day_bounds_utc, local_today,
                             uptime_days, uptime_row, write_uptime_csvs)
from tests.dataset_fixture import build_db

UTC = dt.timezone.utc
REPO = Path(__file__).resolve().parents[1]

GOLDEN_UPTIME = (
    "service_date,expected_minutes,ok_minutes,uptime_fraction\n"
    "2026-03-23,1440,2,0.001389\n"
)


def test_uptime_columns_match_the_spec():
    assert UPTIME_COLUMNS == ("service_date", "expected_minutes", "ok_minutes",
                              "uptime_fraction")


def test_golden_uptime_csv(tmp_path):
    db = build_db()
    days = uptime_days(db, dt.date(2026, 3, 24))
    assert days == [dt.date(2026, 3, 23)]
    written = write_uptime_csvs(db, tmp_path, days)
    assert written == [tmp_path / "uptime" / "2026-03-23.csv"]
    assert written[0].read_bytes() == GOLDEN_UPTIME.encode("utf-8")


def test_duplicate_heartbeats_in_one_minute_count_once():
    # Three ok heartbeats, two of them in the 00:00 bucket -> 2 ok minutes.
    db = build_db()
    assert uptime_row(db, dt.date(2026, 3, 23))["ok_minutes"] == 2


def test_failed_poll_is_not_an_ok_minute():
    db = build_db(heartbeats=[("2026-03-23T00:00:00.100000+00:00", 0)])
    assert uptime_row(db, dt.date(2026, 3, 23))["ok_minutes"] == 0


def test_day_with_no_heartbeats_is_written_as_a_visible_zero(tmp_path):
    # A gap must be published as a zero row, never interpolated or omitted.
    db = build_db()
    days = uptime_days(db, dt.date(2026, 3, 25))
    assert days == [dt.date(2026, 3, 23), dt.date(2026, 3, 24)]
    write_uptime_csvs(db, tmp_path, days)
    gap = (tmp_path / "uptime" / "2026-03-24.csv").read_text(encoding="utf-8")
    assert gap.splitlines()[1] == "2026-03-24,1440,0,0.000000"


def test_day_boundary_is_local_not_utc():
    # 23:30Z on 14 July is 00:30 local (Dublin is UTC+1 in summer), so the
    # heartbeat belongs to service day 2026-07-15.
    db = build_db(heartbeats=[("2026-07-14T23:30:00.000000+00:00", 1)])
    assert uptime_days(db, dt.date(2026, 7, 16)) == [dt.date(2026, 7, 15)]
    assert uptime_row(db, dt.date(2026, 7, 15))["ok_minutes"] == 1
    assert uptime_row(db, dt.date(2026, 7, 14))["ok_minutes"] == 0
    start, end = day_bounds_utc(dt.date(2026, 7, 15))
    assert start == dt.datetime(2026, 7, 14, 23, 0, tzinfo=UTC)
    assert end == dt.datetime(2026, 7, 15, 23, 0, tzinfo=UTC)


def test_empty_heartbeat_table_yields_no_days():
    db = build_db(heartbeats=[])
    assert uptime_days(db, dt.date(2026, 3, 24)) == []


def test_local_today_returns_a_date():
    assert isinstance(local_today(), dt.date)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_published_dataset_paths_are_not_git_ignored():
    """An ignored data/ makes the whole publish pipeline a silent no-op.

    `git add -- data` would stage nothing, `git diff --cached --quiet` would
    exit 0, and the publisher would print "dataset unchanged, nothing to push"
    every night while publishing nothing at all.
    """
    for path in ("data/manifest.json", "data/daily/2026-03-23.csv",
                 "data/uptime/2026-03-23.csv"):
        proc = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO)
        assert proc.returncode == 1, f"{path} is git-ignored"
    # The probe captures stay ignored - they are binary fixtures, not output.
    proc = subprocess.run(["git", "check-ignore", "-q", "data/probe/vehicles.pb"],
                          cwd=REPO)
    assert proc.returncode == 0, "data/probe/ must stay ignored"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_dataset_uptime.py -q`

Expected: FAIL — collection error, `ModuleNotFoundError: No module named 'publish'`, reported as `ERROR tests/test_dataset_uptime.py` with `1 error`.

- [ ] **Step 3: Write minimal implementation**

Create `C:\Users\Alex\Projects\ghost-bus\publish\__init__.py` as an empty file (zero bytes).

Create `C:\Users\Alex\Projects\ghost-bus\publish\dataset.py`:

```python
"""Publish the open dataset: SQLite -> data/daily, data/uptime, data/manifest.json.

Runs on the VM, daily, after the classifier. stdlib only. The site is built in
CI from these files and never from the database (spec D3), so whatever this
module writes is exactly what the public sees. This module never touches git:
committing and pushing is ops/publish.sh's job.
"""
from __future__ import annotations

import csv
import datetime as dt
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1
BASELINE_REQUIRED_DAYS = 14
LOCAL_TZ = "Europe/Dublin"
UTC = dt.timezone.utc

# SUPERSEDED DURING EXECUTION (2026-07-19). This task originally specified a
# flat `EXPECTED_MINUTES_PER_DAY = 1440` and accepted, on the two DST days,
# understating our uptime in spring and CLAMPING TO 1.000000 in autumn - which
# masks up to 60 minutes of real downtime. That error direction is unacceptable
# for a self-accountability metric: tracker downtime EXCLUDES trips precisely so
# we never blame an operator for our own blindness, so a number that can conceal
# our downtime undermines the reason it is published. Implemented instead as a
# per-day `expected_minutes(day)` derived from `day_bounds_utc`'s real span
# (1380 / 1440 / 1500), which the CSV schema already accommodated via its
# `expected_minutes` column. See commit d49f4d4.

UPTIME_COLUMNS = ("service_date", "expected_minutes", "ok_minutes", "uptime_fraction")


def _write_csv(path: Path, columns, rows) -> None:
    """Write a CSV with LF line endings so output is byte-identical on any host
    and git diffs of the published dataset stay clean."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row[column] for column in columns])


def local_today(tz: str = LOCAL_TZ) -> dt.date:
    return dt.datetime.now(ZoneInfo(tz)).date()


def day_bounds_utc(day: dt.date, tz: str = LOCAL_TZ) -> tuple[dt.datetime, dt.datetime]:
    """[start, end) in UTC for one local service day."""
    zone = ZoneInfo(tz)
    nxt = day + dt.timedelta(days=1)
    start = dt.datetime(day.year, day.month, day.day, tzinfo=zone)
    end = dt.datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def uptime_days(db: sqlite3.Connection, today: dt.date) -> list[dt.date]:
    """Every complete local service day from the first heartbeat to yesterday.

    Contiguous by construction: a day with no heartbeats at all is still
    published, as a zero row. A gap in our own coverage is a fact about us and
    is never omitted or interpolated.
    """
    row = db.execute("SELECT MIN(ts_utc) FROM heartbeats").fetchone()
    if row is None or row[0] is None:
        return []
    first = dt.datetime.fromisoformat(row[0]).astimezone(ZoneInfo(LOCAL_TZ)).date()
    last = today - dt.timedelta(days=1)
    if first > last:
        return []
    return [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]


def uptime_row(db: sqlite3.Connection, day: dt.date) -> dict:
    start, end = day_bounds_utc(day)
    # Distinct minute buckets, not raw rows - matches classify.store.uptime, so
    # a crash-loop cannot inflate the published figure.
    (ok_minutes,) = db.execute(
        "SELECT COUNT(DISTINCT substr(ts_utc,1,16)) FROM heartbeats "
        "WHERE ok=1 AND ts_utc>=? AND ts_utc<?",
        (start.isoformat(), end.isoformat())).fetchone()
    expected = expected_minutes(day)  # per-day; see the superseded note above
    fraction = min(1.0, ok_minutes / expected)
    return {"service_date": day.isoformat(),
            "expected_minutes": expected,
            "ok_minutes": ok_minutes,
            "uptime_fraction": f"{fraction:.6f}"}


def write_uptime_csvs(db: sqlite3.Connection, data_dir, days) -> list[Path]:
    written = []
    for day in days:
        path = Path(data_dir) / "uptime" / f"{day.isoformat()}.csv"
        _write_csv(path, UPTIME_COLUMNS, [uptime_row(db, day)])
        written.append(path)
    return written
```

Then edit `C:\Users\Alex\Projects\ghost-bus\.gitignore`. Replace line 6, which is exactly:

```
data/
```

with:

```
data/probe/
```

Leave every other line of `.gitignore` untouched.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_dataset_uptime.py -q; python -m pytest -q`

Expected: PASS — `9 passed` for the file, full suite green with 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```
cd C:\Users\Alex\Projects\ghost-bus
git add publish/__init__.py publish/dataset.py .gitignore tests/dataset_fixture.py tests/test_dataset_uptime.py
git commit -m "feat(publish): uptime CSVs, and stop git-ignoring the dataset

Uptime is derived from distinct ok-minute buckets over local service-day
bounds, so a retry storm cannot inflate it and a day with no heartbeats is
published as a visible zero rather than omitted.

.gitignore line 6 was the bare 'data/', which also ignored every path the
publisher writes: 'git add -- data' would have staged nothing and the nightly
run would have reported 'dataset unchanged' forever. Narrowed to data/probe/,
pinned by a git check-ignore test, because that failure mode is a success
message.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Daily route CSVs with the published schema

**Files:**
- Modify: `C:\Users\Alex\Projects\ghost-bus\publish\dataset.py` (extend the import block; add `DAILY_COLUMNS` after `UPTIME_COLUMNS`; append after `write_uptime_csvs`)
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_dataset_daily.py`

**Precondition:** `aggregate.rollup.route_day_rollup` must already return the split rates and no `ghost_rate`. Check with `python -c "from aggregate.rollup import RATE_KEYS; print(RATE_KEYS)"` — it must print the six split names. If `RATE_KEYS` does not exist, Task 2 is not done — stop, and do not add a compatibility shim here.

**Interfaces:**
- Consumes: `aggregate.rollup.route_day_rollup(db) -> list[dict]` with keys `route_id, service_date, scheduled, excluded, cancelled, completed, vanished, untracked` plus the six `RATE_KEYS`, each `float | None` (`None` when `scheduled - excluded <= 0`).
- Consumes: `timetable/gtfs.py` tables `gtfs_routes(route_id, agency_id, route_short_name, route_long_name)` and `gtfs_agency(agency_id, agency_name)`.
- Consumes from Task 4: `_write_csv`.
- Produces, relied on by Tasks 8-9: `DAILY_COLUMNS: tuple[str, ...]`, `route_names(db) -> dict[str, tuple[str, str, str]]`, `unnamed_routes(db, names) -> list[str]`, `complete_service_days(db, today) -> list[str]`, `daily_rows_by_date(db, names) -> dict[str, list[dict]]`, `daily_rows(db, service_date, names) -> list[dict]`, `write_daily_csvs(db, data_dir, days, names) -> list[Path]`.

`write_daily_csvs` rolls the outcomes table up **once** and buckets by date. `route_day_rollup` reads the whole `trip_outcomes` table with no `WHERE`, so calling it once per published day would re-roll ~9k trips/day × 365 days on a 1 GB VM and never finish. A test pins the single pass.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_dataset_daily.py`:

```python
import datetime as dt
import sqlite3

from publish.dataset import (DAILY_COLUMNS, complete_service_days, daily_rows,
                             route_names, unnamed_routes, write_daily_csvs)
from tests.dataset_fixture import SERVICE_DATE, build_db, consecutive_dates

# Hand-checked Wilson bounds at z=1.96:
#   1/8 -> 0.125000 [0.022417, 0.470895]
#   1/2 -> 0.500000 [0.094529, 0.905471]
#   0/2 -> 0.000000 [0.000000, 0.657628]
GOLDEN_DAILY = (
    "service_date,route_id,route_short_name,route_long_name,agency_name,"
    "scheduled,excluded,cancelled,completed,vanished,untracked,"
    "vanished_rate,vanished_lo,vanished_hi,"
    "untracked_rate,untracked_lo,untracked_hi\n"
    "2026-03-23,03C 120 e a,,,,2,0,0,1,1,0,"
    "0.500000,0.094529,0.905471,0.000000,0.000000,0.657628\n"
    "2026-03-23,R1,1,Fixtureville Main,Fixtureville Bus,10,2,1,5,1,1,"
    "0.125000,0.022417,0.470895,0.125000,0.022417,0.470895\n"
    "2026-03-23,R2,2,Fixtureville Orbital,Fixtureville Bus,1,1,0,0,0,0,"
    ",,,,,\n"
)


def test_daily_columns_match_the_spec_verbatim():
    assert DAILY_COLUMNS == (
        "service_date", "route_id", "route_short_name", "route_long_name",
        "agency_name", "scheduled", "excluded", "cancelled", "completed",
        "vanished", "untracked",
        "vanished_rate", "vanished_lo", "vanished_hi",
        "untracked_rate", "untracked_lo", "untracked_hi")


def test_no_column_sums_the_two_rates():
    # Spec D1: the two rates are never summed by any code path, and no
    # combined field is published under any name.
    for banned in ("ghost_rate", "combined_rate", "unreliable_rate",
                   "vanished_plus_untracked", "failure_rate"):
        assert banned not in DAILY_COLUMNS


def test_golden_daily_csv(tmp_path):
    db = build_db()
    names = route_names(db)
    written = write_daily_csvs(db, tmp_path, [SERVICE_DATE], names)
    assert written == [tmp_path / "daily" / "2026-03-23.csv"]
    assert written[0].read_bytes() == GOLDEN_DAILY.encode("utf-8")


def test_rows_are_sorted_by_route_id():
    db = build_db()
    rows = daily_rows(db, SERVICE_DATE, route_names(db))
    assert [r["route_id"] for r in rows] == ["03C 120 e a", "R1", "R2"]


def test_zero_denominator_publishes_empty_cells_never_zero():
    db = build_db()
    r2 = next(r for r in daily_rows(db, SERVICE_DATE, route_names(db))
              if r["route_id"] == "R2")
    for column in ("vanished_rate", "vanished_lo", "vanished_hi",
                   "untracked_rate", "untracked_lo", "untracked_hi"):
        assert r2[column] == "", column


def test_route_missing_from_gtfs_falls_back_to_raw_id_and_is_listed():
    db = build_db()
    names = route_names(db)
    unnamed = next(r for r in daily_rows(db, SERVICE_DATE, names)
                   if r["route_id"] == "03C 120 e a")
    assert unnamed["route_short_name"] == ""
    assert unnamed["route_long_name"] == ""
    assert unnamed["agency_name"] == ""
    assert unnamed_routes(db, names) == ["03C 120 e a"]


def test_todays_partial_day_is_excluded():
    db = build_db(service_dates=("2026-03-23", "2026-03-24"))
    assert complete_service_days(db, dt.date(2026, 3, 24)) == ["2026-03-23"]
    assert complete_service_days(db, dt.date(2026, 3, 25)) == ["2026-03-23",
                                                               "2026-03-24"]


def test_route_names_survive_a_database_with_no_gtfs_tables():
    db = sqlite3.connect(":memory:")
    db.executescript("""
    CREATE TABLE trip_outcomes (
      trip_id TEXT, service_date TEXT, route_id TEXT, start_utc TEXT, outcome TEXT,
      PRIMARY KEY (trip_id, service_date));
    """)
    db.execute("INSERT INTO trip_outcomes VALUES ('t','2026-03-23','R9',"
               "'2026-03-23T07:00:00+00:00','COMPLETED')")
    db.commit()
    assert route_names(db) == {}
    # Every route then surfaces as unnamed rather than being silently dropped.
    assert unnamed_routes(db, {}) == ["R9"]


def test_write_daily_csvs_rolls_the_outcomes_table_up_once(tmp_path, monkeypatch):
    """One full-table rollup per run, not one per published day.

    route_day_rollup SELECTs the whole trip_outcomes table with no WHERE. A
    year of Dublin-scale data called once per day would be ~3M rows re-rolled
    365 times on a 1 GB VM.
    """
    import publish.dataset as dataset

    real = dataset.route_day_rollup
    calls = []

    def counting(db):
        calls.append(1)
        return real(db)

    monkeypatch.setattr(dataset, "route_day_rollup", counting)
    days = consecutive_dates(14)
    db = build_db(service_dates=days)
    dataset.write_daily_csvs(db, tmp_path, days, {})
    assert len(calls) == 1
    assert len(list((tmp_path / "daily").iterdir())) == 14
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_dataset_daily.py -q`

Expected: FAIL — collection error, `ImportError: cannot import name 'DAILY_COLUMNS' from 'publish.dataset'`, reported as `ERROR tests/test_dataset_daily.py` with `1 error`.

- [ ] **Step 3: Write minimal implementation**

In `C:\Users\Alex\Projects\ghost-bus\publish\dataset.py`, replace the import block:

```python
import csv
import datetime as dt
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo
```

with:

```python
import csv
import datetime as dt
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from aggregate.rollup import route_day_rollup
```

Then add, immediately after the `UPTIME_COLUMNS = (...)` line:

```python
DAILY_COLUMNS = ("service_date", "route_id", "route_short_name", "route_long_name",
                 "agency_name", "scheduled", "excluded", "cancelled", "completed",
                 "vanished", "untracked",
                 "vanished_rate", "vanished_lo", "vanished_hi",
                 "untracked_rate", "untracked_lo", "untracked_hi")
```

Then append to the end of the file:

```python
def _fmt_rate(value: float | None) -> str:
    """An undefined rate is published as an empty cell, never as 0.0."""
    return "" if value is None else f"{value:.6f}"


def route_names(db: sqlite3.Connection) -> dict[str, tuple[str, str, str]]:
    """route_id -> (short name, long name, agency name), all non-null strings."""
    try:
        rows = db.execute(
            "SELECT r.route_id, COALESCE(r.route_short_name,''), "
            "COALESCE(r.route_long_name,''), COALESCE(a.agency_name,'') "
            "FROM gtfs_routes r LEFT JOIN gtfs_agency a ON a.agency_id = r.agency_id"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        # A database predating the timetable load simply has no names yet; every
        # route then lands in unnamed_routes, which is published. Anything else
        # (I/O error, corruption) must crash rather than quietly blank the names.
        if "no such table" in str(exc):
            return {}
        raise
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def unnamed_routes(db: sqlite3.Connection, names: dict) -> list[str]:
    """Route ids in trip_outcomes with no gtfs_routes row - surfaced, not dropped."""
    seen = {r for (r,) in db.execute("SELECT DISTINCT route_id FROM trip_outcomes")}
    return sorted(seen - set(names))


def complete_service_days(db: sqlite3.Connection, today: dt.date) -> list[str]:
    """Spec D7: only service days strictly before today (Europe/Dublin). A
    partial day understates trip counts and distorts every rate built on it."""
    return [d for (d,) in db.execute(
        "SELECT DISTINCT service_date FROM trip_outcomes "
        "WHERE service_date < ? ORDER BY service_date", (today.isoformat(),))]


def _daily_row(r: dict, names: dict) -> dict:
    short, long_name, agency = names.get(r["route_id"], ("", "", ""))
    return {
        "service_date": r["service_date"],
        "route_id": r["route_id"],
        "route_short_name": short,
        "route_long_name": long_name,
        "agency_name": agency,
        "scheduled": r["scheduled"],
        "excluded": r["excluded"],
        "cancelled": r["cancelled"],
        "completed": r["completed"],
        "vanished": r["vanished"],
        "untracked": r["untracked"],
        "vanished_rate": _fmt_rate(r["vanished_rate"]),
        "vanished_lo": _fmt_rate(r["vanished_lo"]),
        "vanished_hi": _fmt_rate(r["vanished_hi"]),
        "untracked_rate": _fmt_rate(r["untracked_rate"]),
        "untracked_lo": _fmt_rate(r["untracked_lo"]),
        "untracked_hi": _fmt_rate(r["untracked_hi"]),
    }


def daily_rows_by_date(db: sqlite3.Connection, names: dict) -> dict[str, list[dict]]:
    """Every publishable row, bucketed by service_date, from ONE full rollup.

    route_day_rollup materialises the whole trip_outcomes table, so it is called
    exactly once here and the result is indexed. Calling it per day would make
    the nightly run quadratic in published history.
    """
    by_date: dict[str, list[dict]] = {}
    for r in route_day_rollup(db):
        by_date.setdefault(r["service_date"], []).append(_daily_row(r, names))
    for rows in by_date.values():
        # Explicit, so row order is this module's own guarantee rather than an
        # inherited property of the rollup's internal sort.
        rows.sort(key=lambda row: row["route_id"])
    return by_date


def daily_rows(db: sqlite3.Connection, service_date: str, names: dict) -> list[dict]:
    return daily_rows_by_date(db, names).get(service_date, [])


def write_daily_csvs(db: sqlite3.Connection, data_dir, days,
                     names: dict) -> list[Path]:
    by_date = daily_rows_by_date(db, names)
    written = []
    for day in days:
        path = Path(data_dir) / "daily" / f"{day}.csv"
        _write_csv(path, DAILY_COLUMNS, by_date.get(day, []))
        written.append(path)
    return written
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_dataset_daily.py -q; python -m pytest -q`

Expected: PASS — `9 passed` for the file, full suite green with 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```
cd C:\Users\Alex\Projects\ghost-bus
git add publish/dataset.py tests/test_dataset_daily.py
git commit -m "feat(publish): daily route CSVs with split vanished/untracked rates

One CSV per complete service day, carrying both rates and both Wilson
intervals as separate columns and no combined field under any name (D1). An
undefined rate is an empty cell, never 0.0. A route absent from gtfs_routes
keeps its raw id and is listed rather than dropped.

The outcomes table is rolled up once per run and bucketed by date: calling
route_day_rollup per published day re-reads the whole table and would not
finish on the VM after a year of history.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Publish-gate evaluation and timetable provenance

**Files:**
- Modify: `C:\Users\Alex\Projects\ghost-bus\publish\dataset.py` (extend the import block; append after `write_daily_csvs`)
- Modify: `C:\Users\Alex\Projects\ghost-bus\timetable\gtfs.py:129` (add one statement after the `gtfs_hash` insert)
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_dataset_gate.py`

**Interfaces:**
- Consumes: `run_checks.check_conservation(db) -> dict`, `run_checks.check_outcomes_valid(db) -> dict`, `run_checks.check_rates_bounded(db) -> dict`, each returning `{"check": str, "passed": bool, "violations": list}`.
- Consumes: `timetable/gtfs.py` table `gtfs_meta(key, value)`.
- Produces, relied on by Task 8: `run_gate(db) -> dict[str, bool]`, `timetable_hash(db) -> str`, `timetable_loaded_at(db) -> str`, `_count(db, sql) -> int`.
- Produces: `timetable.gtfs.load_gtfs` additionally writes `gtfs_meta` key `gtfs_loaded_at`, an aware-UTC ISO-8601 string with second precision.

The about-data page is required by the spec to state the timetable's load date, not just its hash. Nothing recorded that date, so `load_gtfs` starts recording it. Databases loaded before this change have no such key and report an empty string, which the site renders as an em dash — visibly unknown rather than silently blank or invented.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_dataset_gate.py`:

```python
import datetime as dt
import sqlite3

from publish.dataset import run_gate, timetable_hash, timetable_loaded_at
from tests.dataset_fixture import GTFS_HASH, GTFS_LOADED_AT, build_db
from tests.fixtureville import build_gtfs_zip
from timetable.gtfs import load_gtfs


def test_run_gate_reports_all_three_checks():
    db = build_db()
    assert run_gate(db) == {"conservation": True, "rates_bounded": True,
                            "outcomes_valid": True}


def test_run_gate_short_circuits_on_an_invalid_outcome():
    # check_conservation and check_rates_bounded would KeyError on an unknown
    # outcome, so an invalid-outcome database must never reach them.
    db = build_db()
    db.execute("INSERT INTO trip_outcomes VALUES "
               "('bad','2026-03-23','R1','2026-03-23T20:00:00+00:00','MAYBE')")
    db.commit()
    assert run_gate(db) == {"conservation": False, "rates_bounded": False,
                            "outcomes_valid": False}


def test_timetable_hash_reads_gtfs_meta():
    assert timetable_hash(build_db()) == GTFS_HASH


def test_timetable_hash_missing_is_an_empty_string():
    db = build_db()
    db.execute("DELETE FROM gtfs_meta")
    db.commit()
    assert timetable_hash(db) == ""


def test_provenance_survives_a_database_with_no_gtfs_meta_table():
    db = sqlite3.connect(":memory:")
    assert timetable_hash(db) == ""
    assert timetable_loaded_at(db) == ""


def test_timetable_loaded_at_reads_gtfs_meta():
    assert timetable_loaded_at(build_db()) == GTFS_LOADED_AT


def test_timetable_loaded_at_missing_is_an_empty_string():
    # A database loaded before this key existed must degrade to "unknown",
    # never to a fabricated date.
    db = build_db()
    db.execute("DELETE FROM gtfs_meta WHERE key='gtfs_loaded_at'")
    db.commit()
    assert timetable_loaded_at(db) == ""


def test_load_gtfs_records_when_the_timetable_was_loaded(tmp_path):
    conn = sqlite3.connect(":memory:")
    zip_path = tmp_path / "f.zip"
    build_gtfs_zip(zip_path)
    load_gtfs(zip_path, conn)
    stamp = timetable_loaded_at(conn)
    parsed = dt.datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == dt.timedelta(0)
    assert parsed.microsecond == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_dataset_gate.py -q`

Expected: FAIL — collection error, `ImportError: cannot import name 'run_gate' from 'publish.dataset'`, reported as `ERROR tests/test_dataset_gate.py` with `1 error`.

- [ ] **Step 3: Write minimal implementation**

In `C:\Users\Alex\Projects\ghost-bus\publish\dataset.py`, replace the import block:

```python
import csv
import datetime as dt
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from aggregate.rollup import route_day_rollup
```

with:

```python
import csv
import datetime as dt
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from aggregate.rollup import route_day_rollup
from run_checks import check_conservation, check_outcomes_valid, check_rates_bounded
```

Then append to the end of `publish/dataset.py`:

```python
def run_gate(db: sqlite3.Connection) -> dict[str, bool]:
    """The publish gate, in the same order run_checks.main uses.

    outcomes_valid runs first and short-circuits: conservation and
    rates_bounded key into per-outcome dict slots, so an unrecognized outcome
    string would KeyError there instead of failing cleanly. The two are
    reported as False in that case, which never reaches the manifest - a failed
    gate writes nothing at all (see write_dataset).
    """
    if not check_outcomes_valid(db)["passed"]:
        return {"conservation": False, "rates_bounded": False,
                "outcomes_valid": False}
    return {"conservation": check_conservation(db)["passed"],
            "rates_bounded": check_rates_bounded(db)["passed"],
            "outcomes_valid": True}


def _count(db: sqlite3.Connection, sql: str) -> int:
    try:
        (n,) = db.execute(sql).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0
        raise
    return n


def _meta(db: sqlite3.Connection, key: str) -> str:
    try:
        row = db.execute("SELECT value FROM gtfs_meta WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return ""
        raise
    return row[0] if row and row[0] else ""


def timetable_hash(db: sqlite3.Connection) -> str:
    return _meta(db, "gtfs_hash")


def timetable_loaded_at(db: sqlite3.Connection) -> str:
    """When the current timetable was loaded, or "" if we never recorded it.

    Databases loaded before load_gtfs started writing this key report "", which
    the about-data page renders as an em dash. An absent fact is shown as
    unknown; it is never back-filled with a guess.
    """
    return _meta(db, "gtfs_loaded_at")
```

Then, in `C:\Users\Alex\Projects\ghost-bus\timetable\gtfs.py`, find line 129:

```python
    db.execute("INSERT OR REPLACE INTO gtfs_meta VALUES ('gtfs_hash', ?)", (digest,))
```

and insert immediately after it:

```python
    # The about-data page has to state when the timetable was loaded, not just
    # which one it is. Second precision: this is provenance, not telemetry.
    db.execute("INSERT OR REPLACE INTO gtfs_meta VALUES ('gtfs_loaded_at', ?)",
               (dt.datetime.now(UTC).replace(microsecond=0).isoformat(),))
```

(`dt` and `UTC` are already imported at the top of `timetable/gtfs.py`; add no new imports there.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_dataset_gate.py -q; python -m pytest -q`

Expected: PASS — `8 passed` for the file, full suite green with 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```
cd C:\Users\Alex\Projects\ghost-bus
git add publish/dataset.py timetable/gtfs.py tests/test_dataset_gate.py
git commit -m "feat(publish): gate evaluation and timetable provenance

run_gate runs outcomes_valid first and short-circuits, because the other two
checks index per-outcome dict slots and would KeyError on an unknown outcome
rather than failing cleanly.

load_gtfs now records gtfs_loaded_at alongside gtfs_hash: the about-data page
is required to state when the timetable was loaded, and nothing recorded it.
A database loaded before this key existed reports an empty string, which the
site renders as an em dash - unknown, never guessed.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: `publish/slugs.py` — route id slugification with a deterministic, stable collision rule

**Files:**
- Create: `C:\Users\Alex\Projects\ghost-bus\publish\slugs.py`
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_site_slugs.py`

`publish/__init__.py` already exists from Task 4. Do not recreate it.

**Interfaces:**
- Consumes: nothing. This module imports no other project module, deliberately.
- Produces: `slugify(route_id: str) -> str`, `slug_map(route_ids: Iterable[str], existing: dict[str, str] | None = None) -> dict[str, str]`.

**Why its own module, and why it comes before the manifest.** The route-id → slug map is *published data*: `publish/dataset.py` writes it into `data/manifest.json` on the VM (Task 8) and `publish/site.py` reads it back in CI (Task 16). Both need these two functions and neither may import the other — `dataset.py` opens the database and must never be reachable from the CI-only build path (D3/D4), and `site.py` must never reach the database. A third, dependency-free module is the only shape that satisfies both, so it is built before the first task that needs it.

Collision rule, stated once and pinned: `slug_map` iterates `sorted(set(route_ids))`. Any route id present in `existing` keeps the slug it was published under, provided nothing else has already claimed it. The remaining ids are assigned in sorted order; the first claimant of a bare slug keeps it, and later collisions get `-2`, `-3`, … appended until the slug is free. An empty slug (a route id made entirely of punctuation) becomes `route` and falls into the same numbering. A route id in `existing` that is *not* in `route_ids` is dropped from the result and reserves nothing — carrying retired routes forward is the publisher's job (Task 8), not this function's.

`existing` is what makes route URLs stable. Without it, a new route id that sorts *before* an incumbent and slugifies the same way would demote the incumbent from `03c-120-e-a` to `03c-120-e-a-2`, and its published URL would silently move. `publish/dataset.py` feeds the previously published map in.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_site_slugs.py`:

```python
from publish.slugs import slug_map, slugify


def test_slugify_lowercases_and_replaces_spaces():
    assert slugify("03C 120 e a") == "03c-120-e-a"


def test_slugify_collapses_runs_and_strips_edges():
    assert slugify("  46A//  Ballsbridge  ") == "46a-ballsbridge"


def test_slugify_of_pure_punctuation_is_route():
    assert slugify("///") == "route"


def test_slugify_is_pure_ascii_and_filename_safe():
    slug = slugify("Route <1> / éire")
    assert all(c.isalnum() or c == "-" for c in slug)
    assert slug == "route-1-ire"


def test_slug_map_is_stable_and_collision_free():
    ids = ["03C 120 e a", "03c-120-e-a", "03C/120/e/a", "zzz"]
    first = slug_map(ids)
    second = slug_map(list(reversed(ids)))
    assert first == second
    assert len(set(first.values())) == len(first)


def test_slug_map_collision_numbering_is_sorted_order():
    ids = ["03C/120/e/a", "03C 120 e a", "03c-120-e-a"]
    got = slug_map(ids)
    # sorted(set(ids)) == ['03C 120 e a', '03C/120/e/a', '03c-120-e-a']
    assert got["03C 120 e a"] == "03c-120-e-a"
    assert got["03C/120/e/a"] == "03c-120-e-a-2"
    assert got["03c-120-e-a"] == "03c-120-e-a-3"


def test_slug_map_handles_empty_slug_collisions():
    got = slug_map(["///", "!!!"])
    assert sorted(got.values()) == ["route", "route-2"]


def test_slug_map_keeps_a_previously_published_slug():
    """Published URLs must not move when a new route id arrives.

    "03C 120 e a" sorts before "03C/120/e/a" (0x20 < 0x2F), so without the
    existing map the newcomer would take the bare slug and demote the route
    that has been live under it.
    """
    got = slug_map(["03C 120 e a", "03C/120/e/a"],
                   existing={"03C/120/e/a": "03c-120-e-a"})
    assert got["03C/120/e/a"] == "03c-120-e-a"
    assert got["03C 120 e a"] == "03c-120-e-a-2"


def test_slug_map_ignores_an_existing_entry_for_a_route_that_is_gone():
    got = slug_map(["zzz"], existing={"vanished-route": "zzz", "zzz": "zzz-9"})
    # The retired route reserves nothing; the live one keeps its published slug.
    # publish/dataset.py is what carries a retired route's slug forward, by
    # passing its id back in alongside the live ones (Task 8).
    assert got == {"zzz": "zzz-9"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_slugs.py -q`

Expected: FAIL — collection error, `ModuleNotFoundError: No module named 'publish.slugs'`, reported as `ERROR tests/test_site_slugs.py` with `1 error`.

- [ ] **Step 3: Write minimal implementation**

Create `C:\Users\Alex\Projects\ghost-bus\publish\slugs.py`:

```python
"""Route id -> URL slug, shared by the publisher and the site builder.

The slug map is published in data/manifest.json by publish/dataset.py and read
back by publish/site.py in CI. Both sides must agree byte for byte, and neither
may import the other (the publisher opens the database; the builder must never
be able to), so the rule lives here on its own. stdlib only, no project imports.
"""
from __future__ import annotations

import re
from typing import Iterable

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(route_id: str) -> str:
    """Return a filename-safe slug for a GTFS route id.

    Production route ids contain spaces and punctuation, e.g. "03C 120 e a".
    Lowercase, replace every run of non-alphanumerics with a single hyphen,
    strip leading and trailing hyphens. A route id with no alphanumerics at all
    slugifies to "route"; slug_map resolves the collisions that follow.
    """
    slug = _NON_SLUG.sub("-", route_id.strip().lower()).strip("-")
    return slug or "route"


def slug_map(route_ids: Iterable[str],
             existing: dict[str, str] | None = None) -> dict[str, str]:
    """Map every route id to a unique slug, deterministically and stably.

    A route id listed in `existing` keeps the slug it was published under, so
    long as nothing else has claimed it first. Everything else is assigned in
    sorted order: the first claimant of a bare slug keeps it, later collisions
    get "-2", "-3", ... appended.

    Without `existing`, a new route id sorting before an incumbent and
    slugifying the same way would take the bare slug and move the incumbent's
    published URL. publish/dataset.py feeds the previously published map in for
    exactly that reason.
    """
    ids = sorted(set(route_ids))
    mapping: dict[str, str] = {}
    used: set[str] = set()

    for route_id in ids:
        slug = (existing or {}).get(route_id)
        if slug and slug not in used:
            mapping[route_id] = slug
            used.add(slug)

    for route_id in ids:
        if route_id in mapping:
            continue
        base = slugify(route_id)
        slug = base
        suffix = 2
        while slug in used:
            slug = f"{base}-{suffix}"
            suffix += 1
        used.add(slug)
        mapping[route_id] = slug

    return {route_id: mapping[route_id] for route_id in ids}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_slugs.py -q; python -m pytest -q`

Expected: PASS — `9 passed` for the file, full suite green with 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```
cd C:\Users\Alex\Projects\ghost-bus
git add publish/slugs.py tests/test_site_slugs.py
git commit -m "feat(publish): deterministic, stable route id slugification

Production GTFS route ids contain spaces (\"03C 120 e a\"), so route page
filenames need slugs. slug_map iterates sorted(set(ids)) and appends -2, -3 on
collision, making the map independent of caller ordering, and honours an
existing map first so a newly appearing route id cannot take a slug that is
already live and move a published URL.

Its own module because the map is published data: the VM writes it into
data/manifest.json and CI reads it back to build the site, so both publish and
site need the rule while neither may import the other.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: `manifest.json`, the published slug map, and the 14-day baseline gate, in both directions

**Files:**
- Modify: `C:\Users\Alex\Projects\ghost-bus\publish\dataset.py` (extend the import block; append after `timetable_loaded_at`)
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_dataset_manifest.py`

**Interfaces:**
- Consumes from Tasks 4-6: `SCHEMA_VERSION`, `BASELINE_REQUIRED_DAYS`, `UTC`, `local_today`, `uptime_days`, `write_uptime_csvs`, `route_names`, `unnamed_routes`, `complete_service_days`, `write_daily_csvs`, `run_gate`, `timetable_hash`, `timetable_loaded_at`, `_count`.
- Consumes from Task 7: `publish.slugs.slug_map`.
- Produces, relied on by Task 9: `build_manifest(db, days, gate, names, slugs, now_utc) -> dict`, `write_manifest(data_dir, manifest) -> Path`, `write_dataset(db, data_dir, *, today=None, now_utc=None) -> dict`, `published_route_ids(db, days) -> list[str]`, `read_published_slugs(data_dir) -> dict[str, str]`, `published_slugs(route_ids, previous) -> dict[str, str]`.
- Produces the published manifest contract `publish/site.py` reads, with keys in this exact order: `schema_version`, `generated_at`, `timetable_hash`, `timetable_loaded_at`, `coverage` (`first_day`, `last_day`, `complete_days`), `scoreboard_ready`, `baseline_required_days`, `gate` (`conservation`, `rates_bounded`, `outcomes_valid`), `counts` (`observations`, `snapshots`, `trips_classified`), `unnamed_routes`, `route_slugs`.

**The slug map belongs to the dataset, not to the site output.** The site is only ever built by GitHub Actions (D4, Task 19), which checks out into a brand-new `_site` on an ephemeral runner every run. A map kept beside the previous *build* would therefore always read back empty in production and the stable-URL guarantee would never engage — a route page's public URL could move whenever the operator's route ids changed. The map is written here, into `data/manifest.json`, which CI checks out with the data, so the builder always sees the real previous map. Once a route page has a URL, that URL is permanent.

**Retired routes keep their slugs.** `slug_map` drops entries for ids it was not asked about — correct for assignment, since a route that no longer runs must not reserve a slug against a live one. But a link to a withdrawn route must keep resolving rather than being handed to a different route, so `published_slugs` feeds the retired ids back in alongside the live ones: they keep the slug they were published under, and by being in the map they still reserve it. `slug_map`'s own semantics are unchanged.

**The baseline gate is a state, not an event.** Below 14 complete days, `write_dataset` writes the manifest and the uptime CSVs and **removes** `data/daily/` if it exists. `data/` on the VM is a working copy of what is already public, so an additive-only gate would leave every previously published route CSV standing next to a page that says we publish nothing about any route — a restored database, a repaired table, or a `service_date` correction is enough to trigger it.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_dataset_manifest.py`:

```python
import datetime as dt
import json

from publish.dataset import (BASELINE_REQUIRED_DAYS, build_manifest,
                             published_slugs, route_names, write_dataset)
from tests.dataset_fixture import (GTFS_HASH, GTFS_LOADED_AT, SERVICE_DATE,
                                   build_db, consecutive_dates)

UTC = dt.timezone.utc
FIXED_NOW = dt.datetime(2026, 3, 24, 4, 15, 0, tzinfo=UTC)


def read_manifest(data_dir):
    return json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))


def test_manifest_has_exactly_the_spec_keys(tmp_path):
    db = build_db()
    write_dataset(db, tmp_path, today=dt.date(2026, 3, 24), now_utc=FIXED_NOW)
    manifest = read_manifest(tmp_path)
    assert list(manifest) == ["schema_version", "generated_at", "timetable_hash",
                              "timetable_loaded_at", "coverage", "scoreboard_ready",
                              "baseline_required_days", "gate", "counts",
                              "unnamed_routes", "route_slugs"]
    assert list(manifest["coverage"]) == ["first_day", "last_day", "complete_days"]
    assert list(manifest["gate"]) == ["conservation", "rates_bounded",
                                      "outcomes_valid"]
    assert list(manifest["counts"]) == ["observations", "snapshots",
                                        "trips_classified"]


def test_manifest_values_for_a_single_complete_day(tmp_path):
    db = build_db()
    write_dataset(db, tmp_path, today=dt.date(2026, 3, 24), now_utc=FIXED_NOW)
    assert read_manifest(tmp_path) == {
        "schema_version": 1,
        "generated_at": "2026-03-24T04:15:00+00:00",
        "timetable_hash": GTFS_HASH,
        "timetable_loaded_at": GTFS_LOADED_AT,
        "coverage": {"first_day": SERVICE_DATE, "last_day": SERVICE_DATE,
                     "complete_days": 1},
        "scoreboard_ready": False,
        "baseline_required_days": 14,
        "gate": {"conservation": True, "rates_bounded": True,
                 "outcomes_valid": True},
        "counts": {"observations": 3, "snapshots": 3, "trips_classified": 13},
        "unnamed_routes": ["03C 120 e a"],
        "route_slugs": {"03C 120 e a": "03c-120-e-a", "R1": "r1", "R2": "r2"},
    }


def test_manifest_file_is_pretty_printed_and_newline_terminated(tmp_path):
    db = build_db()
    write_dataset(db, tmp_path, today=dt.date(2026, 3, 24), now_utc=FIXED_NOW)
    text = (tmp_path / "manifest.json").read_text(encoding="utf-8")
    assert text.startswith('{\n  "schema_version": 1,\n')
    assert text.endswith("\n")
    assert "\r" not in text


def test_thirteen_complete_days_publish_no_route_csvs(tmp_path):
    days = consecutive_dates(13)          # 2026-03-02 .. 2026-03-14
    db = build_db(service_dates=days)
    write_dataset(db, tmp_path, today=dt.date(2026, 3, 15), now_utc=FIXED_NOW)
    manifest = read_manifest(tmp_path)
    assert manifest["coverage"]["complete_days"] == 13
    assert manifest["scoreboard_ready"] is False
    assert not (tmp_path / "daily").exists()


def test_fourteen_complete_days_flip_the_scoreboard_on(tmp_path):
    days = consecutive_dates(14)          # 2026-03-02 .. 2026-03-15
    db = build_db(service_dates=days)
    write_dataset(db, tmp_path, today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    manifest = read_manifest(tmp_path)
    assert manifest["coverage"] == {"first_day": "2026-03-02",
                                    "last_day": "2026-03-15",
                                    "complete_days": 14}
    assert manifest["scoreboard_ready"] is True
    assert BASELINE_REQUIRED_DAYS == 14
    written = sorted(p.name for p in (tmp_path / "daily").iterdir())
    assert written == [f"{d}.csv" for d in days]


def test_falling_below_the_baseline_withdraws_published_route_csvs(tmp_path):
    """The gate is a state, not an event.

    data/ is a working copy of what is already public. If coverage falls back
    below the threshold, route data must be withdrawn, not left standing beside
    a page that says we publish nothing about any route.
    """
    write_dataset(build_db(service_dates=consecutive_dates(14)), tmp_path,
                  today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    assert list((tmp_path / "daily").iterdir())
    write_dataset(build_db(service_dates=consecutive_dates(13)), tmp_path,
                  today=dt.date(2026, 3, 15), now_utc=FIXED_NOW)
    assert not (tmp_path / "daily").exists()
    assert read_manifest(tmp_path)["scoreboard_ready"] is False


def test_uptime_is_exempt_from_the_baseline_gate(tmp_path):
    # Day one: no route data may ship, but our own downtime always does.
    db = build_db()
    write_dataset(db, tmp_path, today=dt.date(2026, 3, 24), now_utc=FIXED_NOW)
    assert not (tmp_path / "daily").exists()
    assert (tmp_path / "uptime" / "2026-03-23.csv").exists()


def test_empty_database_yields_null_coverage(tmp_path):
    db = build_db(service_dates=(), heartbeats=[])
    write_dataset(db, tmp_path, today=dt.date(2026, 3, 24), now_utc=FIXED_NOW)
    manifest = read_manifest(tmp_path)
    assert manifest["coverage"] == {"first_day": None, "last_day": None,
                                    "complete_days": 0}
    assert manifest["scoreboard_ready"] is False


def test_build_manifest_is_pure_and_writes_nothing(tmp_path):
    db = build_db()
    names = route_names(db)
    manifest = build_manifest(db, [SERVICE_DATE],
                              {"conservation": True, "rates_bounded": True,
                               "outcomes_valid": True}, names,
                              {"R1": "r1"}, FIXED_NOW)
    assert manifest["generated_at"] == "2026-03-24T04:15:00+00:00"
    assert manifest["route_slugs"] == {"R1": "r1"}
    assert list(tmp_path.iterdir()) == []


def test_a_new_route_cannot_take_a_slug_published_to_an_incumbent():
    """Assignment must honour what is already public.

    "03C 120 e a" sorts before "03C/120/e/a" (0x20 < 0x2F), so on a fresh
    assignment the newcomer would take the bare slug the incumbent is already
    live under, and a published route URL would move.
    """
    got = published_slugs(["03C 120 e a", "03C/120/e/a"],
                          {"03C/120/e/a": "03c-120-e-a"})
    assert got["03C/120/e/a"] == "03c-120-e-a"
    assert got["03C 120 e a"] == "03c-120-e-a-2"


def test_a_retired_routes_slug_is_carried_forward_and_never_reassigned():
    """A withdrawn route's URL must keep resolving to that route.

    "GONE" has dropped out of the current window, so slug_map on its own would
    drop it from the map and hand "gone" to the next route that slugifies the
    same way — silently pointing an existing public link at a different route.
    """
    got = published_slugs(["gone", "R1"], {"GONE": "gone", "R1": "r1"})
    assert got["GONE"] == "gone"
    assert got["gone"] == "gone-2"
    assert got["R1"] == "r1"


def test_the_published_slug_map_is_stable_across_two_publishes(tmp_path):
    """Second publish reads the first one's manifest back off disk."""
    expected = {"03C 120 e a": "03c-120-e-a", "R1": "r1", "R2": "r2"}
    first = write_dataset(build_db(service_dates=consecutive_dates(14)), tmp_path,
                          today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    second = write_dataset(build_db(service_dates=consecutive_dates(14)), tmp_path,
                           today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    assert first["route_slugs"] == expected
    assert second["route_slugs"] == expected
    assert read_manifest(tmp_path)["route_slugs"] == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_dataset_manifest.py -q`

Expected: FAIL — collection error, `ImportError: cannot import name 'build_manifest' from 'publish.dataset'`, reported as `ERROR tests/test_dataset_manifest.py` with `1 error`.

- [ ] **Step 3: Write minimal implementation**

In `C:\Users\Alex\Projects\ghost-bus\publish\dataset.py`, replace the import block:

```python
import csv
import datetime as dt
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from aggregate.rollup import route_day_rollup
from run_checks import check_conservation, check_outcomes_valid, check_rates_bounded
```

with:

```python
import csv
import datetime as dt
import json
import shutil
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from aggregate.rollup import route_day_rollup
from publish.slugs import slug_map
from run_checks import check_conservation, check_outcomes_valid, check_rates_bounded
```

Then append to the end of the file:

```python
def published_route_ids(db: sqlite3.Connection, days: list[str]) -> list[str]:
    """Every route id appearing in the published service days."""
    if not days:
        return []
    # A range, not an IN list: `days` grows by one per day forever and would
    # eventually blow past SQLite's bound-parameter limit.
    return sorted({r for (r,) in db.execute(
        "SELECT DISTINCT route_id FROM trip_outcomes "
        "WHERE service_date BETWEEN ? AND ?", (days[0], days[-1]))})


def read_published_slugs(data_dir) -> dict[str, str]:
    """The route_slugs map from the manifest we published last time.

    The map lives in the dataset rather than beside the previous site build
    because the site is rebuilt from scratch on an ephemeral CI runner every
    run: a map kept in the site output would always read back empty, and a
    route page's public URL could move whenever route ids changed. data/ is a
    working copy of what is already public, so this file is the real previous
    map. A missing or unreadable manifest means "nothing published yet".
    """
    path = Path(data_dir) / "manifest.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("route_slugs") or {}
    except (ValueError, OSError):
        return {}


def published_slugs(route_ids, previous: dict[str, str]) -> dict[str, str]:
    """The map to publish: every current route, plus every route ever published.

    Retired route ids are fed back through slug_map alongside the live ones, so
    they keep the slug they were published under and go on reserving it. A link
    to a route that has since been withdrawn therefore still resolves to that
    route, and can never be silently handed to a different one. slug_map's own
    rule is unchanged - on its own it drops ids it was not asked about, which is
    exactly why the carry-forward is done here and not there.
    """
    return slug_map(set(route_ids) | set(previous), existing=previous)


def build_manifest(db: sqlite3.Connection, days: list[str], gate: dict,
                   names: dict, slugs: dict, now_utc: dt.datetime) -> dict:
    """The machine-readable description of this release. Pure: writes nothing."""
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_utc.astimezone(UTC).replace(microsecond=0).isoformat(),
        "timetable_hash": timetable_hash(db),
        "timetable_loaded_at": timetable_loaded_at(db),
        "coverage": {"first_day": days[0] if days else None,
                     "last_day": days[-1] if days else None,
                     "complete_days": len(days)},
        "scoreboard_ready": len(days) >= BASELINE_REQUIRED_DAYS,
        "baseline_required_days": BASELINE_REQUIRED_DAYS,
        "gate": {"conservation": gate["conservation"],
                 "rates_bounded": gate["rates_bounded"],
                 "outcomes_valid": gate["outcomes_valid"]},
        # The poller archives exactly one snapshot per successful poll, and
        # writes an ok=1 heartbeat in the same step, so ok heartbeats are the
        # snapshot count without walking the archive directory.
        "counts": {"observations": _count(db, "SELECT COUNT(*) FROM observations"),
                   "snapshots": _count(db, "SELECT COUNT(*) FROM heartbeats WHERE ok=1"),
                   "trips_classified": _count(db, "SELECT COUNT(*) FROM trip_outcomes")},
        "unnamed_routes": unnamed_routes(db, names),
        # Published here, not in the site output: CI checks this file out and
        # rebuilds _site from scratch every run, so this is the only copy that
        # survives to keep route URLs where they are.
        "route_slugs": dict(slugs),
    }


def write_manifest(data_dir, manifest: dict) -> Path:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
    return path


def write_dataset(db: sqlite3.Connection, data_dir, *,
                  today: dt.date | None = None,
                  now_utc: dt.datetime | None = None) -> dict:
    """Write the whole published dataset and return the manifest describing it."""
    data_dir = Path(data_dir)
    today = local_today() if today is None else today
    now_utc = dt.datetime.now(UTC) if now_utc is None else now_utc
    gate = run_gate(db)
    names = route_names(db)
    days = complete_service_days(db, today)
    # Read the previous map BEFORE write_manifest overwrites it below.
    slugs = published_slugs(published_route_ids(db, days),
                            read_published_slugs(data_dir))

    # Uptime is deliberately exempt from the 14-day baseline gate (spec D6): it
    # is our own downtime, not a claim about any operator, and the site's
    # pre-baseline mode depends on it being published from day one.
    write_uptime_csvs(db, data_dir, uptime_days(db, today))

    daily_dir = data_dir / "daily"
    if len(days) >= BASELINE_REQUIRED_DAYS:
        write_daily_csvs(db, data_dir, days, names)
    elif daily_dir.is_dir():
        # The baseline gate is a state, not an event: if coverage falls back
        # below it, previously published route data is WITHDRAWN, not left
        # standing next to a page saying we publish nothing about any route.
        shutil.rmtree(daily_dir)

    manifest = build_manifest(db, days, gate, names, slugs, now_utc)
    write_manifest(data_dir, manifest)
    return manifest
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_dataset_manifest.py -q; python -m pytest -q`

Expected: PASS — `12 passed` for the file, full suite green with 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```
cd C:\Users\Alex\Projects\ghost-bus
git add publish/dataset.py tests/test_dataset_manifest.py
git commit -m "feat(publish): manifest.json, the published slug map, and the 14-day gate

The manifest carries schema version, generation time, timetable hash and load
date, coverage, gate results, counts, every route id we could not name, and the
route id to URL slug map.

route_slugs lives in the dataset, not in the site output: the site is built
only by CI, which checks out into a brand-new _site on an ephemeral runner, so
a map kept beside the previous build would always read back empty and published
route URLs would move whenever route ids changed. Retired route ids are carried
forward so a link to a withdrawn route keeps resolving instead of being
reassigned to a different one.

The baseline gate works in both directions: below 14 complete days nothing
route-level is written, and any previously published data/daily is removed.
data/ is a working copy of what is already public, so an additive-only gate
would leave route CSVs linked from a page that says we publish none. Uptime is
exempt and ships from day one.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: Publish-gate enforcement and the dataset CLI

**Files:**
- Modify: `C:\Users\Alex\Projects\ghost-bus\publish\dataset.py` (extend the import block; add `GateFailed`; edit `write_dataset`; append the CLI)
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_dataset_cli.py`

**Interfaces:**
- Consumes: `ghostbus_config.get_db(path=None) -> sqlite3.Connection` (sets `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=30000`).
- Consumes from Task 8: `run_gate`, `write_dataset`.
- Produces: `GateFailed(Exception)`; `main(argv: list[str] | None = None) -> int`, runnable as `python -m publish.dataset --db <path> --data-dir <path>`.

**This module never touches git.** There is no `--commit` flag and no `_git_publish` helper, and none may be added. `ops/publish.sh` is the single VM entry point that commits and pushes: it holds the dirty-tree guard, `GIT_TERMINAL_PROMPT=0`, and the `GIT_ASKPASS` wiring that a bare `subprocess.run(["git", "push"])` from here would have no way to supply. A test pins the absence.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_dataset_cli.py`:

```python
import datetime as dt
import inspect
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import publish.dataset as dataset
from publish.dataset import GateFailed, write_dataset
from tests.dataset_fixture import build_db, consecutive_dates, outcome_rows

UTC = dt.timezone.utc
FIXED_NOW = dt.datetime(2026, 3, 16, 4, 15, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parent.parent

_FILE_SCHEMA = """
CREATE TABLE trip_outcomes (
  trip_id TEXT, service_date TEXT, route_id TEXT, start_utc TEXT, outcome TEXT,
  PRIMARY KEY (trip_id, service_date));
CREATE TABLE heartbeats (ts_utc TEXT PRIMARY KEY, ok INTEGER);
"""


def make_file_db(path: Path, days, extra_rows=()):
    db = sqlite3.connect(path)
    db.executescript(_FILE_SCHEMA)
    for day in days:
        db.executemany("INSERT INTO trip_outcomes VALUES (?,?,?,?,?)",
                       outcome_rows(day))
    db.executemany("INSERT INTO trip_outcomes VALUES (?,?,?,?,?)", extra_rows)
    db.execute("INSERT INTO heartbeats VALUES ('2026-03-02T00:00:00.100000+00:00',1)")
    db.commit()
    db.close()
    return path


def tree(root: Path):
    return {str(p.relative_to(root)).replace("\\", "/"): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_gate_failure_writes_nothing_and_raises(tmp_path):
    db = build_db()
    db.execute("INSERT INTO trip_outcomes VALUES "
               "('bad','2026-03-23','R1','2026-03-23T20:00:00+00:00','MAYBE')")
    db.commit()
    data_dir = tmp_path / "data"
    with pytest.raises(GateFailed):
        write_dataset(db, data_dir, today=dt.date(2026, 3, 24), now_utc=FIXED_NOW)
    assert not data_dir.exists()


def test_gate_failure_leaves_the_previous_publish_untouched(tmp_path):
    # Stale but verified data stays up; it is never replaced by numbers that
    # failed their own checks.
    data_dir = tmp_path / "data"
    write_dataset(build_db(service_dates=consecutive_dates(14)), data_dir,
                  today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    before = tree(data_dir)
    bad = build_db(service_dates=consecutive_dates(14))
    bad.execute("INSERT INTO trip_outcomes VALUES "
                "('bad','2026-03-02','R1','2026-03-02T20:00:00+00:00','MAYBE')")
    bad.commit()
    with pytest.raises(GateFailed):
        write_dataset(bad, data_dir, today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    assert tree(data_dir) == before


def test_cli_gate_failure_exits_nonzero_and_leaves_no_files(tmp_path):
    dbfile = make_file_db(
        tmp_path / "bad.db", consecutive_dates(14),
        extra_rows=[("bad", "2026-03-02", "R1",
                     "2026-03-02T20:00:00+00:00", "MAYBE")])
    data_dir = tmp_path / "data"
    proc = subprocess.run(
        [sys.executable, "-m", "publish.dataset", "--db", str(dbfile),
         "--data-dir", str(data_dir)],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr
    assert "wrote nothing" in proc.stderr
    assert not data_dir.exists()


def test_cli_happy_path_writes_the_dataset_and_exits_zero(tmp_path):
    dbfile = make_file_db(tmp_path / "good.db", consecutive_dates(14))
    data_dir = tmp_path / "data"
    proc = subprocess.run(
        [sys.executable, "-m", "publish.dataset", "--db", str(dbfile),
         "--data-dir", str(data_dir)],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (data_dir / "manifest.json").exists()
    assert len(list((data_dir / "daily").iterdir())) == 14


def test_cli_exposes_exactly_the_flags_the_publisher_uses():
    proc = subprocess.run(
        [sys.executable, "-m", "publish.dataset", "--help"],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--db" in proc.stdout
    assert "--data-dir" in proc.stdout
    assert "--commit" not in proc.stdout


def test_the_dataset_module_never_touches_git():
    """Committing and pushing belongs to ops/publish.sh, which owns the
    dirty-tree guard and the GIT_ASKPASS credential path. A bare git push from
    here would have no credential source on the VM at all."""
    assert not hasattr(dataset, "_git_publish")
    source = inspect.getsource(dataset)
    assert "subprocess" not in source
    assert "push" not in source


def test_two_runs_are_byte_identical(tmp_path):
    days = consecutive_dates(14)
    first, second = tmp_path / "first", tmp_path / "second"
    write_dataset(build_db(service_dates=days), first,
                  today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    write_dataset(build_db(service_dates=days), second,
                  today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    assert tree(first) == tree(second)


def test_rerunning_into_the_same_directory_is_idempotent(tmp_path):
    days = consecutive_dates(14)
    data_dir = tmp_path / "data"
    db = build_db(service_dates=days)
    write_dataset(db, data_dir, today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    before = tree(data_dir)
    write_dataset(db, data_dir, today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    assert tree(data_dir) == before
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_dataset_cli.py -q`

Expected: FAIL — collection error, `ImportError: cannot import name 'GateFailed' from 'publish.dataset'`, reported as `ERROR tests/test_dataset_cli.py` with `1 error`.

- [ ] **Step 3: Write minimal implementation**

In `C:\Users\Alex\Projects\ghost-bus\publish\dataset.py`, replace the import block:

```python
import csv
import datetime as dt
import json
import shutil
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from aggregate.rollup import route_day_rollup
from publish.slugs import slug_map
from run_checks import check_conservation, check_outcomes_valid, check_rates_bounded
```

with:

```python
import argparse
import csv
import datetime as dt
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from aggregate.rollup import route_day_rollup
from ghostbus_config import get_db
from publish.slugs import slug_map
from run_checks import check_conservation, check_outcomes_valid, check_rates_bounded


class GateFailed(Exception):
    """The publish gate did not pass, so nothing at all was written."""
```

Then, inside `write_dataset`, replace:

```python
    gate = run_gate(db)
    names = route_names(db)
```

with:

```python
    # The gate runs before the first mkdir: a failed gate must leave the
    # previously published dataset in place, untouched, rather than replace it
    # with numbers nothing has verified.
    gate = run_gate(db)
    if not all(gate.values()):
        failed = ", ".join(sorted(k for k, ok in gate.items() if not ok))
        raise GateFailed(failed)
    names = route_names(db)
```

Then append to the end of the file:

```python
def main(argv: list[str] | None = None) -> int:
    """Write the dataset, or write nothing and exit 1.

    This CLI never invokes git. ops/publish.sh commits and pushes; keeping the
    two apart is what lets a gate failure stop the run before any repository is
    touched at all.
    """
    parser = argparse.ArgumentParser(
        description="Publish the Ghost Bus dataset from SQLite to CSV + manifest.")
    parser.add_argument("--db", default="state/ghostbus.db",
                        help="path to the SQLite database (default: state/ghostbus.db)")
    parser.add_argument("--data-dir", default="data",
                        help="directory to write into (default: data)")
    args = parser.parse_args(argv)
    db = get_db(args.db)
    try:
        manifest = write_dataset(db, Path(args.data_dir))
    except GateFailed as exc:
        print(f"FAIL publish gate: {exc}", file=sys.stderr)
        print("wrote nothing", file=sys.stderr)
        return 1
    print(f"published {manifest['coverage']['complete_days']} complete days, "
          f"scoreboard_ready={manifest['scoreboard_ready']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_dataset_cli.py -q; python -m pytest -q`

Expected: PASS — `8 passed` for the file, full suite green with 0 failed, 0 errors.

Then confirm the CLI surface the VM publisher depends on:

```
python -m publish.dataset --help
```
Expected: usage text listing `--db` and `--data-dir`, and no `--commit`.

- [ ] **Step 5: Commit**

```
cd C:\Users\Alex\Projects\ghost-bus
git add publish/dataset.py tests/test_dataset_cli.py
git commit -m "feat(publish): enforce the publish gate and add the dataset CLI

A failed gate raises before the first mkdir, so nothing is written and the
previously published data - stale but verified - stays exactly where it was.
The CLI exits 1 with 'wrote nothing' on stderr and no traceback.

The CLI never touches git and has no --commit flag: ops/publish.sh owns the
push, along with the dirty-tree guard and the GIT_ASKPASS credential path that
a bare git push from here could not supply. A test pins the absence.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: `publish/site.py` foundations — page shell, stylesheet, escaping and formatting helpers

**Files:**
- Create: `C:\Users\Alex\Projects\ghost-bus\publish\site.py`
- Create: `C:\Users\Alex\Projects\ghost-bus\site\base.html.tmpl`
- Create: `C:\Users\Alex\Projects\ghost-bus\site\style.css`
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_site_shell.py`

`publish/__init__.py` already exists from Task 4. Do not recreate it.

**Interfaces:**
- Consumes: `aggregate.rates.rate_with_interval` and `publish.slugs.slug_map` (imported here so every later site task inherits them; `rate_with_interval` is first used in Task 12, `slug_map` in Task 16). `publish/site.py` never imports `publish/dataset.py` — the builder must not be able to reach the database (D3/D4), which is why the slug rule is its own module.
- Produces: `SITE_DIR: Path`, `EM_DASH`, `EN_DASH`, `COUNT_FIELDS`, `RATE_FIELDS`, `esc(value) -> str`, `fmt_pct(value: float | None) -> str`, `fmt_rate(interval) -> str`, `fmt_interval(interval) -> str`, `route_label(entry: dict) -> str`, `load_template(name: str, site_dir=SITE_DIR) -> string.Template`, `render_nav(root: str, current: str) -> str`, `render_page(site_dir, *, title, root, current, generated_at, content) -> str`.
- Produces the import block every later site task extends, in this exact order: `csv`, `datetime as dt`, `html`, `json`, `re`, `pathlib.Path`, `string.Template`, then `from aggregate.rates import rate_with_interval` and `from publish.slugs import slug_map`.

`root` is `""` for top-level pages and `"../"` for pages under `route/`, so every link stays relative and the site works from any path on GitHub Pages. No page ever references a host other than its own.

**Markup budget.** Later tasks add a build-time tag allowlist over every emitted page. Use only these elements anywhere in this plan's templates and renderers: `html head meta title link body header nav main footer section h1 h2 h3 p a ul ol li table thead tbody tr th td dl dt dd span strong em code small abbr br hr`. There is deliberately no `div`.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_site_shell.py`:

```python
import re

from publish.site import (
    EM_DASH, SITE_DIR, esc, fmt_interval, fmt_pct, fmt_rate, load_template,
    render_nav, render_page, route_label,
)


def test_esc_neutralises_angle_brackets_and_quotes():
    assert esc('<script>alert("x")</script>') == \
        "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;"


def test_esc_of_none_is_empty_string():
    assert esc(None) == ""


def test_esc_escapes_single_quotes_too():
    assert esc("it's") == "it&#x27;s"


def test_fmt_pct_and_rate_use_one_decimal():
    assert fmt_pct(0.066667) == "6.7%"
    assert fmt_rate((0.04, 0.0204, 0.0769)) == "4.0%"


def test_zero_trial_rate_renders_as_em_dash_never_zero():
    assert fmt_rate(None) == EM_DASH
    assert fmt_interval(None) == EM_DASH
    assert fmt_pct(None) == EM_DASH
    assert "0.0" not in fmt_rate(None)


def test_fmt_interval_uses_an_en_dash_range():
    assert fmt_interval((0.04, 0.0204, 0.0769)) == "2.0–7.7%"


def test_route_label_prefers_short_name_and_falls_back_to_id():
    assert route_label({"route_id": "03C 120 e a", "route_short_name": "120"}) == "120"
    assert route_label({"route_id": "03C 120 e a", "route_short_name": ""}) == "03C 120 e a"


def test_render_nav_marks_the_current_page():
    nav = render_nav("", "methodology.html")
    assert '<a href="methodology.html" aria-current="page">Methodology</a>' in nav
    assert '<a href="index.html">Scoreboard</a>' in nav


def test_render_nav_prefixes_root_for_subdirectory_pages():
    nav = render_nav("../", "index.html")
    assert '<a href="../methodology.html">Methodology</a>' in nav


def test_render_page_escapes_the_title_and_embeds_content():
    page = render_page(SITE_DIR, title="<b>hi</b>", root="", current="index.html",
                       generated_at="2026-07-20T04:00:00+00:00",
                       content="<p>body text</p>")
    assert "<title>&lt;b&gt;hi&lt;/b&gt; — Ghost Bus</title>" in page
    assert "<p>body text</p>" in page
    assert page.startswith("<!doctype html>")
    assert 'lang="en"' in page and 'charset="utf-8"' in page


def test_page_makes_no_third_party_requests():
    page = render_page(SITE_DIR, title="x", root="", current="index.html",
                       generated_at="now", content="")
    assert "http://" not in page and "https://" not in page
    assert "//" not in re.sub(r"<!--.*?-->", "", page).replace("<!doctype", "")
    assert "<script" not in page.lower()


def test_stylesheet_is_local_and_self_contained():
    css = (SITE_DIR / "style.css").read_text(encoding="utf-8")
    assert "@import" not in css
    assert "url(" not in css
    assert "http" not in css


def test_load_template_reads_utf8():
    tmpl = load_template("base.html.tmpl")
    assert "—" in tmpl.template
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_shell.py -q`

Expected: FAIL — collection error, `ModuleNotFoundError: No module named 'publish.site'`, reported as `ERROR tests/test_site_shell.py` with `1 error`.

- [ ] **Step 3: Write minimal implementation**

Create `C:\Users\Alex\Projects\ghost-bus\site\base.html.tmpl` (UTF-8, no BOM):

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title} — Ghost Bus</title>
<link rel="stylesheet" href="${root}style.css">
</head>
<body>
<header class="site-header">
<a class="brand" href="${root}index.html">Ghost Bus</a>
<nav class="site-nav">${nav}</nav>
</header>
<main>
${content}
</main>
<footer class="site-footer">
<p>Timetable and real-time data from Transport for Ireland / National Transport Authority. Ghost Bus is not affiliated with TFI, the NTA, or any operator.</p>
<p>Page built from the published dataset. Data generated ${generated_at}.</p>
</footer>
</body>
</html>
```

Create `C:\Users\Alex\Projects\ghost-bus\site\style.css`:

```css
:root {
  --ink: #16181d;
  --muted: #5b6270;
  --rule: #d8dbe2;
  --bad: #8c1c13;
  --ok: #2f6f4f;
  --gap: #c9ccd3;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  color: var(--ink);
  background: #fdfdfc;
}
main, .site-header, .site-footer { max-width: 62rem; margin: 0 auto; padding: 0 1.25rem; }
.site-header {
  display: flex; align-items: baseline; gap: 1.5rem; flex-wrap: wrap;
  padding-top: 1.5rem; padding-bottom: 1rem; border-bottom: 1px solid var(--rule);
}
.brand { font-weight: 700; letter-spacing: -0.01em; text-decoration: none; color: var(--ink); }
.site-nav a { margin-right: 1rem; color: var(--muted); text-decoration: none; }
.site-nav a:hover, .site-nav a[aria-current="page"] { color: var(--ink); text-decoration: underline; }
h1 { font-size: 1.9rem; letter-spacing: -0.02em; margin: 2rem 0 0.5rem; }
h2 { font-size: 1.25rem; margin: 2.25rem 0 0.5rem; }
h3 { font-size: 1.05rem; margin: 1.5rem 0 0.35rem; }
.lede { font-size: 1.05rem; color: var(--muted); margin-top: 0; }
.note, .meta { color: var(--muted); font-size: 0.9rem; }
table.board, table.days { border-collapse: collapse; width: 100%; margin: 0.75rem 0 1.5rem; }
table.board th, table.board td, table.days th, table.days td {
  text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid var(--rule);
  vertical-align: baseline;
}
th { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td.interval { color: var(--muted); font-size: 0.9rem; }
td.pos { color: var(--muted); font-variant-numeric: tabular-nums; }
td.route a { color: var(--ink); text-decoration: none; }
td.route a:hover { text-decoration: underline; }
.long { color: var(--muted); font-weight: 400; }
tr.gap td { color: var(--gap); font-style: italic; }
.uptime-strip { list-style: none; display: flex; gap: 3px; padding: 0; margin: 0.5rem 0 0.75rem; }
.uptime-strip .day { width: 14px; height: 30px; border-radius: 2px; background: var(--ok); }
.uptime-strip .degraded { background: #b8860b; }
.uptime-strip .down { background: var(--bad); }
.uptime-strip .gap { background: var(--gap); }
.sr-only {
  position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap;
}
.baseline { border: 1px solid var(--rule); border-left: 4px solid var(--muted); padding: 1rem 1.25rem; margin: 1.5rem 0; }
.site-footer { color: var(--muted); font-size: 0.85rem; border-top: 1px solid var(--rule); margin-top: 3rem; padding: 1.25rem; }
code { font: 0.9em ui-monospace, SFMono-Regular, Menlo, monospace; background: #f1f2f4; padding: 0.05em 0.3em; border-radius: 3px; }
dl.facts dt { font-weight: 600; margin-top: 0.75rem; }
dl.facts dd { margin: 0.1rem 0 0; color: var(--muted); }
ul.files { line-height: 1.7; }
```

Create `C:\Users\Alex\Projects\ghost-bus\publish\site.py`:

```python
"""Build the public scoreboard site from the PUBLISHED CSVs.

This module runs in CI, never on the VM, and never opens the database. Its only
inputs are data/manifest.json, data/daily/*.csv and data/uptime/*.csv, so a
number on the site cannot differ from the number in the downloadable data
(design decision D3). stdlib only (D5): string.Template plus html.escape().
"""
from __future__ import annotations

import csv
import datetime as dt
import html
import json
import re
from pathlib import Path
from string import Template

from aggregate.rates import rate_with_interval
from publish.slugs import slug_map

COUNT_FIELDS = ("scheduled", "excluded", "cancelled", "completed", "vanished", "untracked")
RATE_FIELDS = (
    "vanished_rate", "vanished_lo", "vanished_hi",
    "untracked_rate", "untracked_lo", "untracked_hi",
)

SITE_DIR = Path(__file__).resolve().parent.parent / "site"
EM_DASH = "—"
EN_DASH = "–"

_NAV = (
    ("index.html", "Scoreboard"),
    ("methodology.html", "Methodology"),
    ("about-data.html", "About the data"),
)


def esc(value) -> str:
    """Escape an externally-sourced string before it goes anywhere near a template.

    Route names, long names, agency names and route ids all come from GTFS.
    Escaping them is a security requirement (D5), not a nicety, and
    tests/test_site_escaping.py pins it.
    """
    return html.escape("" if value is None else str(value), quote=True)


def fmt_pct(value: float | None) -> str:
    return EM_DASH if value is None else f"{value * 100:.1f}%"


def fmt_rate(interval) -> str:
    """The point estimate, or an em dash when there were no trials at all."""
    return EM_DASH if interval is None else fmt_pct(interval[0])


def fmt_interval(interval) -> str:
    if interval is None:
        return EM_DASH
    _, lo, hi = interval
    return f"{lo * 100:.1f}{EN_DASH}{hi * 100:.1f}%"


def route_label(entry: dict) -> str:
    return (entry.get("route_short_name") or "") or entry["route_id"]


def load_template(name: str, site_dir=SITE_DIR) -> Template:
    return Template((Path(site_dir) / name).read_text(encoding="utf-8"))


def render_nav(root: str, current: str) -> str:
    items = []
    for href, label in _NAV:
        marker = ' aria-current="page"' if href == current else ""
        items.append(f'<a href="{esc(root + href)}"{marker}>{esc(label)}</a>')
    return " ".join(items)


def render_page(site_dir, *, title: str, root: str, current: str,
                generated_at: str, content: str) -> str:
    """Wrap already-built (and already-escaped) content in the site shell."""
    base = load_template("base.html.tmpl", site_dir)
    return base.substitute(
        title=esc(title),
        root=esc(root),
        nav=render_nav(root, current),
        generated_at=esc(generated_at),
        content=content,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_shell.py -q; python -m pytest -q`

Expected: PASS — `13 passed` for the file, full suite green with 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```
cd C:\Users\Alex\Projects\ghost-bus
git add publish/site.py site/base.html.tmpl site/style.css tests/test_site_shell.py
git commit -m "feat(site): page shell, stylesheet and escaping helpers

string.Template plus html.escape() only (D5). One local stylesheet, no JS, no
external requests of any kind - a test asserts the rendered shell contains no
absolute URL and no protocol-relative reference. A rate with zero trials
formats as an em dash so it can never be rendered as 0.0%.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 11: Read the published dataset (manifest, daily CSVs, uptime CSVs)

**Files:**
- Modify: `C:\Users\Alex\Projects\ghost-bus\publish\site.py` (append after `render_page`)
- Create: `C:\Users\Alex\Projects\ghost-bus\tests\site_fixtures.py`
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_site_readers.py`

**Precondition:** the published daily CSV must already carry the split-rate columns. Check with `python -c "from publish.dataset import DAILY_COLUMNS; print(DAILY_COLUMNS)"` — it must list `vanished_rate` … `untracked_hi` and no `ghost_rate`. If it does not, Task 5 is not done — stop.

**Interfaces:**
- Consumes: the layout written by `publish/dataset.py` — `data/manifest.json`; `data/daily/YYYY-MM-DD.csv` with columns `service_date, route_id, route_short_name, route_long_name, agency_name, scheduled, excluded, cancelled, completed, vanished, untracked, vanished_rate, vanished_lo, vanished_hi, untracked_rate, untracked_lo, untracked_hi`; `data/uptime/YYYY-MM-DD.csv` with `service_date, expected_minutes, ok_minutes, uptime_fraction`.
- Consumes from Task 10: `COUNT_FIELDS`, `RATE_FIELDS`.
- Produces: `read_manifest(data_dir) -> dict`, `read_daily(data_dir) -> list[dict]`, `read_uptime(data_dir) -> list[dict]`.
- Produces (test side): `tests/site_fixtures.py` exposing `DAILY_COLUMNS`, `UPTIME_COLUMNS`, `DEFAULT_MANIFEST`, `daily_row(service_date, route_id, **kw)`, `uptime_row(service_date, expected_minutes=1440, ok_minutes=1440, uptime_fraction=None)`, `write_dataset(root, daily_rows=(), uptime_rows=(), manifest=None) -> Path`. Every later site task imports these.

`write_dataset` creates `data/daily/` **only** when it is given daily rows. A pre-baseline dataset must have no `daily/` directory at all — the builder refuses to render a "we publish nothing about any route" page beside one.

- [ ] **Step 1: Write the failing test**

Create the shared fixture helper first — Tasks 12-17 all import it. Create `C:\Users\Alex\Projects\ghost-bus\tests\site_fixtures.py`:

```python
"""Build a fake *published* dataset on disk for site-builder tests.

The site builder reads CSVs, never the database, so its tests need CSVs, not a
sqlite fixture. Everything here writes UTF-8 explicitly: this repo runs on
Windows where the default codec is cp1252.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

DAILY_COLUMNS = [
    "service_date", "route_id", "route_short_name", "route_long_name", "agency_name",
    "scheduled", "excluded", "cancelled", "completed", "vanished", "untracked",
    "vanished_rate", "vanished_lo", "vanished_hi",
    "untracked_rate", "untracked_lo", "untracked_hi",
]
UPTIME_COLUMNS = ["service_date", "expected_minutes", "ok_minutes", "uptime_fraction"]

DEFAULT_MANIFEST = {
    "schema_version": 1,
    "generated_at": "2026-07-20T04:00:00+00:00",
    "timetable_hash": "0f1c9a2b3d4e5f60",
    "timetable_loaded_at": "2026-07-01T02:00:00+00:00",
    "coverage": {"first_day": "2026-06-01", "last_day": "2026-06-28", "complete_days": 28},
    "scoreboard_ready": True,
    "baseline_required_days": 14,
    "gate": {"conservation": True, "rates_bounded": True, "outcomes_valid": True},
    "counts": {"observations": 128400, "snapshots": 40320, "trips_classified": 9111},
    "unnamed_routes": [],
    # The published route-id -> slug map. Empty here so each test states the
    # map it cares about; the builder falls back to computing one for any route
    # id the dataset does not carry.
    "route_slugs": {},
}


def daily_row(service_date, route_id, **kw):
    """A daily CSV row with every column present. Counts default to 0, rates to ''."""
    row = {c: "" for c in DAILY_COLUMNS}
    row["service_date"] = service_date
    row["route_id"] = route_id
    for c in ("scheduled", "excluded", "cancelled", "completed", "vanished", "untracked"):
        row[c] = 0
    row.update(kw)
    return row


def uptime_row(service_date, expected_minutes=1440, ok_minutes=1440, uptime_fraction=None):
    if uptime_fraction is None:
        uptime_fraction = ok_minutes / expected_minutes if expected_minutes else ""
    return {
        "service_date": service_date,
        "expected_minutes": expected_minutes,
        "ok_minutes": ok_minutes,
        "uptime_fraction": uptime_fraction,
    }


def _write_csv(path, columns, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_dataset(root, daily_rows=(), uptime_rows=(), manifest=None):
    """Write a data/ tree and return its Path.

    daily/ is created only when there are daily rows: a pre-baseline dataset
    has no daily directory at all, and the builder refuses to render a
    'we publish nothing about any route' page next to one.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    man = json.loads(json.dumps(DEFAULT_MANIFEST))
    if manifest:
        man.update(manifest)
    (root / "manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")

    by_day = {}
    for row in daily_rows:
        by_day.setdefault(row["service_date"], []).append(row)
    if by_day:
        daily_dir = root / "daily"
        daily_dir.mkdir(exist_ok=True)
        for day, rows in by_day.items():
            _write_csv(daily_dir / f"{day}.csv", DAILY_COLUMNS, rows)

    by_up = {}
    for row in uptime_rows:
        by_up.setdefault(row["service_date"], []).append(row)
    if by_up:
        uptime_dir = root / "uptime"
        uptime_dir.mkdir(exist_ok=True)
        for day, rows in by_up.items():
            _write_csv(uptime_dir / f"{day}.csv", UPTIME_COLUMNS, rows)
    return root
```

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_site_readers.py`:

```python
from tests.site_fixtures import daily_row, uptime_row, write_dataset

from publish.site import read_daily, read_manifest, read_uptime


def test_read_manifest_returns_the_published_json(tmp_path):
    data = write_dataset(tmp_path / "data")
    manifest = read_manifest(data)
    assert manifest["schema_version"] == 1
    assert manifest["coverage"]["complete_days"] == 28


def test_read_daily_coerces_counts_to_int_and_rates_to_float(tmp_path):
    data = write_dataset(
        tmp_path / "data",
        daily_rows=[
            daily_row(
                "2026-06-01", "03C 120 e a",
                route_short_name="120", route_long_name="Main Street",
                agency_name="Dublin Bus",
                scheduled=10, excluded=1, cancelled=0, completed=7, vanished=1, untracked=1,
                vanished_rate=0.1111, vanished_lo=0.0198, vanished_hi=0.4348,
                untracked_rate=0.1111, untracked_lo=0.0198, untracked_hi=0.4348,
            )
        ],
    )
    rows = read_daily(data)
    assert len(rows) == 1
    row = rows[0]
    assert row["route_id"] == "03C 120 e a"
    assert row["scheduled"] == 10 and isinstance(row["scheduled"], int)
    assert row["vanished_rate"] == 0.1111 and isinstance(row["vanished_rate"], float)


def test_read_daily_reads_every_day_file_sorted_by_date(tmp_path):
    data = write_dataset(
        tmp_path / "data",
        daily_rows=[
            daily_row("2026-06-02", "R1", scheduled=2),
            daily_row("2026-06-01", "R1", scheduled=1),
        ],
    )
    assert [r["service_date"] for r in read_daily(data)] == ["2026-06-01", "2026-06-02"]


def test_read_daily_maps_blank_rate_to_none_never_zero(tmp_path):
    data = write_dataset(
        tmp_path / "data",
        daily_rows=[daily_row("2026-06-01", "R1", scheduled=3, excluded=3)],
    )
    row = read_daily(data)[0]
    assert row["vanished_rate"] is None
    assert row["untracked_hi"] is None


def test_read_daily_on_a_dataset_with_no_daily_dir_is_empty(tmp_path):
    data = write_dataset(tmp_path / "data", manifest={"scoreboard_ready": False})
    assert read_daily(data) == []


def test_read_uptime_parses_fractions(tmp_path):
    data = write_dataset(
        tmp_path / "data",
        uptime_rows=[uptime_row("2026-06-01", 1440, 1436)],
    )
    rows = read_uptime(data)
    assert rows[0]["ok_minutes"] == 1436
    assert 0.997 < rows[0]["uptime_fraction"] < 0.998


def test_read_daily_round_trips_through_the_fixture_writer(tmp_path):
    """Later tasks rewrite a dataset by feeding read_daily's output back in."""
    data = write_dataset(tmp_path / "data",
                         daily_rows=[daily_row("2026-06-01", "R1", scheduled=4,
                                               excluded=1, vanished=1, untracked=0,
                                               cancelled=0, completed=2)])
    first = read_daily(data)
    write_dataset(tmp_path / "data", daily_rows=first)
    assert read_daily(data) == first
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_readers.py -q`

Expected: FAIL — collection error, `ImportError: cannot import name 'read_daily' from 'publish.site'`, reported as `ERROR tests/test_site_readers.py` with `1 error`.

- [ ] **Step 3: Write minimal implementation**

Append to the end of `C:\Users\Alex\Projects\ghost-bus\publish\site.py`:

```python
def _to_int(value) -> int:
    if value in ("", None):
        return 0
    return int(value)


def _to_float(value) -> float | None:
    """Blank means undefined, and undefined is never 0.0 (spec failure table)."""
    if value in ("", None):
        return None
    return float(value)


def read_manifest(data_dir) -> dict:
    return json.loads((Path(data_dir) / "manifest.json").read_text(encoding="utf-8"))


def read_daily(data_dir) -> list[dict]:
    """Every row of every data/daily/*.csv, oldest file first.

    An absent daily/ directory is not an error: before the 14-day baseline the
    publisher writes none, and that is the documented state of the dataset.
    """
    directory = Path(data_dir) / "daily"
    if not directory.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(directory.glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as fh:
            for raw in csv.DictReader(fh):
                row = dict(raw)
                for field in COUNT_FIELDS:
                    row[field] = _to_int(row.get(field))
                for field in RATE_FIELDS:
                    row[field] = _to_float(row.get(field))
                rows.append(row)
    return rows


def read_uptime(data_dir) -> list[dict]:
    directory = Path(data_dir) / "uptime"
    if not directory.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(directory.glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as fh:
            for raw in csv.DictReader(fh):
                row = dict(raw)
                row["expected_minutes"] = _to_int(row.get("expected_minutes"))
                row["ok_minutes"] = _to_int(row.get("ok_minutes"))
                row["uptime_fraction"] = _to_float(row.get("uptime_fraction"))
                rows.append(row)
    return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_readers.py -q; python -m pytest -q`

Expected: PASS — `7 passed` for the file, full suite green with 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```
cd C:\Users\Alex\Projects\ghost-bus
git add publish/site.py tests/site_fixtures.py tests/test_site_readers.py
git commit -m "feat(site): read the published dataset from CSV

The site builder's only inputs are the published manifest and CSVs (D3), so it
gets its own readers and never imports publish.dataset. A blank rate parses to
None, never 0.0, so an undefined rate cannot be rendered as zero anywhere
downstream. A missing daily/ directory reads as no rows: that is the
documented pre-baseline state, not an error.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 12: 28-day window aggregation and the Wilson-lower-bound leaderboard

**Files:**
- Modify: `C:\Users\Alex\Projects\ghost-bus\publish\site.py` (add `WINDOW_DAYS`/`MIN_TRIPS` after `RATE_FIELDS`; append after `read_uptime`)
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_site_leaderboard.py`

**Interfaces:**
- Consumes: `aggregate.rates.rate_with_interval(successes, trials) -> tuple[float, float, float] | None` (imported by Task 10); `read_daily` rows (Task 11).
- Produces: `WINDOW_DAYS = 28`, `MIN_TRIPS = 30`, `window_dates(rows, window=WINDOW_DAYS) -> list[str]`, `aggregate_window(rows, window=WINDOW_DAYS) -> list[dict]`, `leaderboard(rows, window=WINDOW_DAYS, min_trips=MIN_TRIPS) -> tuple[list[dict], list[dict]]` (ranked, unranked).
- Each entry dict carries exactly: `route_id, route_short_name, route_long_name, agency_name, days, scheduled, excluded, cancelled, completed, vanished, untracked, trials, vanished_interval, untracked_interval`.

**The ranking gate counts judgeable trips (`scheduled − excluded`), not trips scheduled** — the spec's D6 wording exactly. `trials = scheduled - excluded` is the denominator of both rates and the number the board displays as "Trips judged", so it is also the number the gate reads. Gating on `scheduled` would let a route with 30 scheduled and 29 excluded be ranked on a **single observation**: 100% vanished, Wilson lower bound 0.2065, straight to the top of a public list of the worst routes. A test pins that case in the unranked list.

Ranked order is the Wilson **lower bound** of the **vanished** rate, descending. The point estimate is only a tiebreak below it; the untracked rate has no influence on position at all. Nothing anywhere sums the two rates or the two counts, and a test asserts no entry field equals `vanished + untracked`.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_site_leaderboard.py`:

```python
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
    assert unranked[0]["vanished_interval"][1] == pytest.approx(0.20653997, abs=1e-6)


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
    assert big["vanished_interval"][1] == pytest.approx(0.02040540, abs=1e-8)
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_leaderboard.py -q`

Expected: FAIL — collection error, `ImportError: cannot import name 'MIN_TRIPS' from 'publish.site'`, reported as `ERROR tests/test_site_leaderboard.py` with `1 error`.

- [ ] **Step 3: Write minimal implementation**

In `C:\Users\Alex\Projects\ghost-bus\publish\site.py`, add immediately after the `RATE_FIELDS = (...)` tuple:

```python
WINDOW_DAYS = 28
MIN_TRIPS = 30
```

Append to the end of `publish/site.py`:

```python
def window_dates(rows: list[dict], window: int = WINDOW_DAYS) -> list[str]:
    """The last `window` distinct complete service dates present in the data."""
    return sorted({row["service_date"] for row in rows})[-window:]


def aggregate_window(rows: list[dict], window: int = WINDOW_DAYS) -> list[dict]:
    """Sum per-route counts over the window and recompute both rates on the sum.

    The two rates share a denominator (scheduled - excluded, matching
    aggregate/rollup.py) and are computed independently. They are never added.
    """
    wanted = set(window_dates(rows, window))
    by_route: dict[str, dict] = {}
    for row in rows:
        if row["service_date"] not in wanted:
            continue
        entry = by_route.get(row["route_id"])
        if entry is None:
            entry = {
                "route_id": row["route_id"],
                "route_short_name": row.get("route_short_name") or "",
                "route_long_name": row.get("route_long_name") or "",
                "agency_name": row.get("agency_name") or "",
                "days": 0,
            }
            entry.update({field: 0 for field in COUNT_FIELDS})
            by_route[row["route_id"]] = entry
        entry["days"] += 1
        for field in COUNT_FIELDS:
            entry[field] += row[field]
        for name in ("route_short_name", "route_long_name", "agency_name"):
            if not entry[name] and row.get(name):
                entry[name] = row[name]

    out = []
    for entry in by_route.values():
        trials = entry["scheduled"] - entry["excluded"]
        entry["trials"] = trials
        entry["vanished_interval"] = rate_with_interval(entry["vanished"], trials)
        entry["untracked_interval"] = rate_with_interval(entry["untracked"], trials)
        out.append(entry)
    return sorted(out, key=lambda e: e["route_id"])


def leaderboard(rows: list[dict], window: int = WINDOW_DAYS,
                min_trips: int = MIN_TRIPS) -> tuple[list[dict], list[dict]]:
    """Return (ranked, unranked).

    Ranking requires >= min_trips JUDGEABLE trips in the window (D6): trials,
    i.e. scheduled minus excluded, which is the denominator of both rates and
    the number the board shows. Gating on `scheduled` would let a route with
    30 scheduled and 29 excluded be ranked on one observation.

    Ranked order is the Wilson LOWER bound of the VANISHED rate, descending,
    worst first (D2). The point estimate is only a tiebreak below it, and the
    untracked rate has no influence on position at all.
    """
    ranked, unranked = [], []
    for entry in aggregate_window(rows, window):
        # trials >= 30 already implies a defined interval; the second clause is
        # belt and braces, not a second policy.
        if entry["trials"] >= min_trips and entry["vanished_interval"] is not None:
            ranked.append(entry)
        else:
            unranked.append(entry)
    ranked.sort(key=lambda e: (-e["vanished_interval"][1],
                               -e["vanished_interval"][0], e["route_id"]))
    unranked.sort(key=lambda e: (-e["trials"], e["route_id"]))
    return ranked, unranked
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_leaderboard.py -q; python -m pytest -q`

Expected: PASS — `11 passed` for the file, full suite green with 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```
cd C:\Users\Alex\Projects\ghost-bus
git add publish/site.py tests/test_site_leaderboard.py
git commit -m "feat(site): 28-day leaderboard ranked by the Wilson lower bound

Ranks by the lower bound of the vanished rate only (D2) and pins a case where
the lower bound and the point estimate disagree (2/30 vs 8/200), so a
regression to point-estimate ranking fails the suite.

The 30-trip gate counts trips we could judge - scheduled minus excluded, the
denominator of both rates - not trips scheduled. A route with 30 scheduled and
29 excluded would otherwise be ranked on one observation, at a Wilson lower
bound of 0.21, at the top of a public list of the worst routes. Pinned.

The two rates are never summed, and no entry field carries their sum.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 13: `index.html` — leaderboard table, uptime strip with visible gaps, pre-baseline mode

**Files:**
- Create: `C:\Users\Alex\Projects\ghost-bus\site\index.html.tmpl`
- Modify: `C:\Users\Alex\Projects\ghost-bus\publish\site.py` (append after `leaderboard`)
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_site_index.py`

**Interfaces:**
- Consumes: `leaderboard`, `window_dates`, `publish.slugs.slug_map` (Task 7), `render_page`, `load_template`, `fmt_rate`, `fmt_interval`, `esc`, `route_label`, `EM_DASH`, and the `read_manifest`/`read_uptime` row shapes.
- Produces: `UPTIME_STRIP_DAYS = 30`, `render_uptime_strip(uptime_rows, last_day, days=UPTIME_STRIP_DAYS) -> str`, `render_board(ranked, unranked, slugs) -> str`, `render_index(site_dir, manifest, daily_rows, uptime_rows, ranked, unranked, slugs) -> str`.

Missing-day rule: a calendar day inside the strip with no uptime row renders as a `gap` cell reading "no data" — never interpolated, never filled with a neighbour's value. Sample size appears on every leaderboard row (the "Trips judged" column, `trials`, with scheduled/excluded in its `title`).

**The window line states the days actually behind it.** `WINDOW_DAYS` is 28 but the scoreboard turns on at 14 complete days, so for the first fortnight a hardcoded "Rolling 28 complete service days" would claim double the data it has. The line reports `len(window_dates(daily_rows))`.

**Board copy matches the gate the code enforces:** "fewer than 30 trips we could judge", never "30 scheduled trips". A test pins both directions.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_site_index.py`:

```python
from tests.site_fixtures import DEFAULT_MANIFEST, daily_row, uptime_row

from publish.site import (SITE_DIR, fmt_rate, leaderboard, render_index,
                          render_uptime_strip)
from publish.slugs import slug_map


def one_day(route_id, scheduled, vanished=0, untracked=0, excluded=0,
            day="2026-06-28", **kw):
    return daily_row(day, route_id, scheduled=scheduled, excluded=excluded,
                     vanished=vanished, untracked=untracked, cancelled=0,
                     completed=scheduled - excluded - vanished - untracked, **kw)


def build(daily_rows, uptime_rows=(), manifest=None):
    man = dict(DEFAULT_MANIFEST)
    if manifest:
        man.update(manifest)
    ranked, unranked = leaderboard(list(daily_rows))
    slugs = slug_map(e["route_id"] for e in ranked + unranked)
    return render_index(SITE_DIR, man, list(daily_rows), list(uptime_rows),
                        ranked, unranked, slugs)


def test_ranked_table_lists_routes_worst_lower_bound_first():
    html = build([one_day("SMALL", 30, vanished=2), one_day("BIG", 200, vanished=8)])
    assert html.index(">BIG<") < html.index(">SMALL<")


def test_every_ranked_row_shows_its_sample_size():
    html = build([one_day("R1", 40, excluded=10, vanished=3)])
    assert "Trips judged" in html
    assert 'title="40 scheduled, 10 excluded"' in html
    assert ">30<" in html


def test_untracked_has_its_own_column_and_interval():
    html = build([one_day("R1", 100, vanished=5, untracked=20)])
    assert html.count("95% interval") == 2
    assert "Untracked" in html and "Vanished" in html


def test_below_threshold_routes_appear_separately_with_counts():
    html = build([one_day("R1", 100, vanished=5), one_day("TINY", 12, vanished=1)])
    assert "Not enough data yet" in html
    assert html.index("TINY") > html.index("Not enough data yet")
    assert ">12<" in html


def test_board_copy_matches_the_gate_the_code_enforces():
    html = build([one_day("R1", 100, vanished=5), one_day("TINY", 12, vanished=1)])
    assert "30 trips we could judge" in html
    assert "30 scheduled trips" not in html


def test_zero_trial_route_renders_an_em_dash_not_zero():
    rows = [one_day("ALLX", 40, excluded=40)]
    ranked, unranked = leaderboard(rows)
    assert ranked == []
    # The honest formatting of an undefined rate, asserted at the source.
    assert fmt_rate(unranked[0]["vanished_interval"]) == "—"
    html = build(rows)
    assert "0.0%" not in html
    assert ">0<" in html          # the counts are still shown


def test_window_line_states_the_days_actually_behind_it():
    rows = [one_day("R1", 4, vanished=1, day=f"2026-06-{d:02d}")
            for d in range(1, 15)]
    html = build(rows)
    assert "Rolling 14 complete service days" in html
    assert "Rolling 28" not in html


def test_pre_baseline_mode_emits_no_route_table():
    html = build([], uptime_rows=[uptime_row("2026-06-09")],
                 manifest={"scoreboard_ready": False,
                           "coverage": {"first_day": "2026-06-01",
                                        "last_day": "2026-06-09",
                                        "complete_days": 9}})
    assert "<table" not in html
    assert "collecting baseline" in html.lower()
    assert "day 9 of 14" in html
    assert "uptime-strip" in html


def test_uptime_strip_is_rendered_even_before_baseline():
    html = build([], uptime_rows=[uptime_row("2026-06-09", 1440, 1440)],
                 manifest={"scoreboard_ready": False,
                           "coverage": {"first_day": "2026-06-09",
                                        "last_day": "2026-06-09",
                                        "complete_days": 1}})
    assert "uptime-strip" in html
    assert 'class="day ok"' in html


def test_missing_day_renders_as_a_visible_gap_never_interpolated():
    rows = [uptime_row("2026-06-26", 1440, 1440), uptime_row("2026-06-28", 1440, 1440)]
    strip = render_uptime_strip(rows, "2026-06-28")
    assert 'title="2026-06-27: no data"' in strip
    assert strip.count("day gap") == 28  # 30 cells, 2 of which have data
    assert "2026-06-27: 100.0%" not in strip


def test_uptime_strip_always_has_30_cells():
    strip = render_uptime_strip([uptime_row("2026-06-28")], "2026-06-28")
    assert strip.count("<li") == 30


def test_uptime_strip_classes_reflect_the_fraction():
    rows = [
        uptime_row("2026-06-28", 1440, 1440),
        uptime_row("2026-06-27", 1440, 1360),
        uptime_row("2026-06-26", 1440, 900),
    ]
    strip = render_uptime_strip(rows, "2026-06-28")
    assert 'class="day ok"' in strip
    assert 'class="day degraded"' in strip
    assert 'class="day down"' in strip


def test_index_links_to_the_route_page_by_slug():
    html = build([one_day("03C 120 e a", 40, vanished=2, route_short_name="120")])
    assert 'href="route/03c-120-e-a.html"' in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_index.py -q`

Expected: FAIL — collection error, `ImportError: cannot import name 'render_index' from 'publish.site'`, reported as `ERROR tests/test_site_index.py` with `1 error`.

- [ ] **Step 3: Write minimal implementation**

Create `C:\Users\Alex\Projects\ghost-bus\site\index.html.tmpl` (UTF-8, no BOM):

```html
<h1>Dublin bus reliability</h1>
<p class="lede">Every scheduled trip on every route, classified into exactly one honest outcome. ${window_line}</p>
${baseline_notice}
${board}
<section class="uptime">
<h2>Our own tracker uptime, last 30 days</h2>
<p class="note">This is <em>our</em> reliability, not any operator's. Grey means we were not watching; trips we could not watch are excluded and never counted against anyone.</p>
${uptime_strip}
</section>
<p class="note"><a href="methodology.html">How these numbers are made, and what they do not mean</a> · <a href="about-data.html">Download every figure on this page</a></p>
```

Append to the end of `C:\Users\Alex\Projects\ghost-bus\publish\site.py`:

```python
UPTIME_STRIP_DAYS = 30


def _strip_end(manifest: dict, uptime_rows: list[dict]) -> str:
    """The right-hand edge of the uptime strip: last published day, else today."""
    last = (manifest.get("coverage") or {}).get("last_day")
    if last:
        return last
    if uptime_rows:
        return max(row["service_date"] for row in uptime_rows)
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def render_uptime_strip(uptime_rows: list[dict], last_day: str,
                        days: int = UPTIME_STRIP_DAYS) -> str:
    """One cell per calendar day. A day we published nothing for is a visible
    gap labelled "no data" - it is never interpolated from its neighbours."""
    by_date = {row["service_date"]: row for row in uptime_rows}
    end = dt.date.fromisoformat(last_day)
    cells = []
    for offset in range(days - 1, -1, -1):
        day = (end - dt.timedelta(days=offset)).isoformat()
        row = by_date.get(day)
        fraction = None if row is None else row["uptime_fraction"]
        if fraction is None:
            label = f"{day}: no data"
            cls = "gap"
        else:
            label = f"{day}: {fraction * 100:.1f}% tracker uptime"
            cls = "ok" if fraction >= 0.99 else ("degraded" if fraction >= 0.90 else "down")
        cells.append(
            f'<li class="day {cls}" title="{esc(label)}">'
            f'<span class="sr-only">{esc(label)}</span></li>'
        )
    return '<ul class="uptime-strip">' + "".join(cells) + "</ul>"


def _route_cell(entry: dict, slugs: dict[str, str], root: str = "") -> str:
    href = f"{root}route/{slugs[entry['route_id']]}.html"
    long_name = entry.get("route_long_name") or ""
    tail = f' <span class="long">{esc(long_name)}</span>' if long_name else ""
    return f'<a href="{esc(href)}"><strong>{esc(route_label(entry))}</strong></a>{tail}'


def _trips_cell(entry: dict) -> str:
    title = f"{entry['scheduled']} scheduled, {entry['excluded']} excluded"
    return f'<td class="num" title="{esc(title)}">{esc(entry["trials"])}</td>'


def _ranked_row(position: int, entry: dict, slugs: dict[str, str]) -> str:
    vanished = entry["vanished_interval"]
    untracked = entry["untracked_interval"]
    return (
        "<tr>"
        f'<td class="pos">{esc(position)}</td>'
        f'<td class="route">{_route_cell(entry, slugs)}</td>'
        f"{_trips_cell(entry)}"
        f'<td class="num">{fmt_rate(vanished)}</td>'
        f'<td class="num interval">{fmt_interval(vanished)}</td>'
        f'<td class="num">{fmt_rate(untracked)}</td>'
        f'<td class="num interval">{fmt_interval(untracked)}</td>'
        "</tr>"
    )


def _unranked_row(entry: dict, slugs: dict[str, str]) -> str:
    """Counts only. No rate is claimed for a route below the gate."""
    return (
        "<tr>"
        f'<td class="route">{_route_cell(entry, slugs)}</td>'
        f"{_trips_cell(entry)}"
        f'<td class="num">{esc(entry["vanished"])}</td>'
        f'<td class="num">{esc(entry["untracked"])}</td>'
        "</tr>"
    )


def render_board(ranked: list[dict], unranked: list[dict],
                 slugs: dict[str, str]) -> str:
    parts: list[str] = []
    if ranked:
        parts.append("<h2>Ranked routes</h2>")
        parts.append(
            '<p class="note">Ordered by the <strong>lower bound</strong> of the vanished '
            "rate, worst first, so a route sits above another only where the evidence "
            "supports it. Untracked is shown separately and has no effect on position. "
            "The two rates are never added together — "
            '<a href="methodology.html">here is why</a>.</p>'
        )
        parts.append(
            '<table class="board"><thead><tr>'
            '<th>#</th><th>Route</th><th class="num">Trips judged</th>'
            '<th class="num">Vanished</th><th class="num">95% interval</th>'
            '<th class="num">Untracked</th><th class="num">95% interval</th>'
            "</tr></thead><tbody>"
        )
        for position, entry in enumerate(ranked, start=1):
            parts.append(_ranked_row(position, entry, slugs))
        parts.append("</tbody></table>")
    if unranked:
        parts.append("<h2>Not enough data yet</h2>")
        parts.append(
            '<p class="note">Fewer than 30 trips we could judge in the window — '
            "scheduled trips minus the ones we were not watching. Counts are shown so "
            "you can see exactly what we have; these routes are not ranked and no rate "
            "is claimed for them.</p>"
        )
        parts.append(
            '<table class="board unranked"><thead><tr>'
            '<th>Route</th><th class="num">Trips judged</th>'
            '<th class="num">Vanished</th><th class="num">Untracked</th>'
            "</tr></thead><tbody>"
        )
        for entry in unranked:
            parts.append(_unranked_row(entry, slugs))
        parts.append("</tbody></table>")
    return "\n".join(parts)


def render_index(site_dir, manifest: dict, daily_rows: list[dict],
                 uptime_rows: list[dict], ranked: list[dict],
                 unranked: list[dict], slugs: dict[str, str]) -> str:
    ready = bool(manifest.get("scoreboard_ready"))
    coverage = manifest.get("coverage") or {}
    required = manifest.get("baseline_required_days", 14)
    complete = coverage.get("complete_days", 0)

    if ready:
        dates = window_dates(daily_rows)
        # The window is at most WINDOW_DAYS, but the board turns on at 14
        # complete days. Printing the constant would claim twice the data we
        # have for the first fortnight.
        count = len(dates)
        span = f"{dates[0]} to {dates[-1]}" if dates else "no complete days yet"
        plural = "" if count == 1 else "s"
        window_line = f"Rolling {count} complete service day{plural}, {span}."
        baseline_notice = ""
        board = render_board(ranked, unranked, slugs)
    else:
        window_line = "No route numbers are published yet."
        baseline_notice = (
            '<section class="baseline">'
            f"<h2>Collecting baseline — day {esc(complete)} of {esc(required)}</h2>"
            "<p>We publish nothing about any route until we have at least "
            f"{esc(required)} complete days of tracking. Ranking routes on a few days of "
            "data would be an accusation the data cannot support, so the table stays "
            "empty until the baseline exists. Our own uptime is published from day one: "
            "it is our reliability, not anyone else's, and you should be able to check "
            "how much we were actually watching.</p>"
            '<p class="note"><a href="methodology.html">Read the methodology in the '
            "meantime</a> — it is complete, and it will not change quietly once "
            "numbers appear.</p>"
            "</section>"
        )
        board = ""

    content = load_template("index.html.tmpl", site_dir).substitute(
        window_line=esc(window_line),
        baseline_notice=baseline_notice,
        board=board,
        uptime_strip=render_uptime_strip(uptime_rows, _strip_end(manifest, uptime_rows)),
    )
    return render_page(
        site_dir,
        title="Scoreboard",
        root="",
        current="index.html",
        generated_at=manifest.get("generated_at", ""),
        content=content,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_index.py -q; python -m pytest -q`

Expected: PASS — `13 passed` for the file, full suite green with 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```
cd C:\Users\Alex\Projects\ghost-bus
git add site/index.html.tmpl publish/site.py tests/test_site_index.py
git commit -m "feat(site): index page, uptime strip and pre-baseline mode

The board shows sample size on every row and untracked in its own column with
its own interval. Below-threshold routes are listed separately with counts and
no rate at all, and the copy says 'fewer than 30 trips we could judge' to match
the gate the code actually enforces.

The window line reports the days actually behind it rather than the 28-day
constant: the board turns on at 14 complete days, so the constant would claim
double the data for the first fortnight.

Before the baseline the page renders the uptime strip and a 'day N of 14' line
and no route table. A day with no uptime row is a visible gap, never
interpolated.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 14: `route/<slug>.html` — per-route detail with day-by-day gaps

**Files:**
- Create: `C:\Users\Alex\Projects\ghost-bus\site\route.html.tmpl`
- Modify: `C:\Users\Alex\Projects\ghost-bus\publish\site.py` (append after `render_index`)
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_site_route_pages.py`

**Interfaces:**
- Consumes: entries from `aggregate_window`/`leaderboard`, `window_dates`, `render_page`, `load_template`, `fmt_rate`, `fmt_interval`, `esc`, `route_label`, `EM_DASH`, `publish.slugs.slug_map` (Task 7).
- Produces: `render_route(site_dir, manifest, entry, daily_rows, slugs, position=None) -> str`.

Two honesty rules, both tested, both consequences of the index copy:

1. **Below the gate the headline percentage is withheld.** The index says no rate is claimed for an unranked route; a detail page printing "8.3%" under a header reading "not ranked" would claim and disclaim the same number, and the 8.3% is the figure that gets screenshotted. Counts and the interval are still shown — at small n the interval's width is the honest signal.
2. **The day-by-day table carries counts only, never per-day percentages.** A single service day is a tiny sample by construction; a rate on it would be the same overclaim at a finer grain.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_site_route_pages.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_route_pages.py -q`

Expected: FAIL — collection error, `ImportError: cannot import name 'render_route' from 'publish.site'`, reported as `ERROR tests/test_site_route_pages.py` with `1 error`.

- [ ] **Step 3: Write minimal implementation**

Create `C:\Users\Alex\Projects\ghost-bus\site\route.html.tmpl` (UTF-8, no BOM):

```html
<h1>Route ${route_name}</h1>
<p class="lede">${route_long}</p>
<p class="meta">Route id <code>${route_id}</code> · ${agency} · ${rank_line}</p>
<h2>${window_heading}</h2>
<dl class="facts">
<dt>Trips judged</dt><dd>${trials} of ${scheduled} scheduled (${excluded} excluded because we were not watching)</dd>
<dt>Vanished — a vehicle was seen and then stopped reporting mid-route</dt><dd>${vanished_count} trips, ${vanished_rate} (95% interval ${vanished_interval})</dd>
<dt>Untracked — no vehicle was ever seen, which is not the same as “did not run”</dt><dd>${untracked_count} trips, ${untracked_rate} (95% interval ${untracked_interval})</dd>
<dt>Cancelled by the operator</dt><dd>${cancelled} trips</dd>
<dt>Completed</dt><dd>${completed} trips</dd>
</dl>
<p class="note">The two rates are reported separately and are never added together. <a href="../methodology.html">Why that matters</a>.</p>
<h2>Day by day</h2>
<p class="note">Counts only: one service day is far too small a sample to put a percentage on, and a day we published nothing for is shown as a gap and never filled in from the days either side of it.</p>
${daily_table}
<p class="note"><a href="../index.html">Back to the scoreboard</a> · <a href="../methodology.html">Methodology</a> · <a href="../about-data.html">Download the data</a></p>
```

Append to the end of `C:\Users\Alex\Projects\ghost-bus\publish\site.py`:

```python
def _daily_table(entry: dict, daily_rows: list[dict]) -> str:
    """One row per calendar day across the window.

    Counts only - a single service day is a sample of a few dozen trips at
    best, and putting a percentage on it would be the same overclaim the
    30-trip gate exists to prevent. Days we did not publish are gap rows: no
    numbers, no interpolation.
    """
    dates = window_dates(daily_rows)
    if not dates:
        return '<p class="note">No complete service days published yet.</p>'
    by_date = {
        row["service_date"]: row
        for row in daily_rows
        if row["route_id"] == entry["route_id"]
    }
    start = dt.date.fromisoformat(dates[0])
    end = dt.date.fromisoformat(dates[-1])

    parts = [
        '<table class="days"><thead><tr>'
        '<th>Day</th><th class="num">Trips judged</th>'
        '<th class="num">Vanished</th><th class="num">Untracked</th>'
        '<th class="num">Cancelled</th><th class="num">Completed</th>'
        "</tr></thead><tbody>"
    ]
    day = end
    while day >= start:
        iso = day.isoformat()
        row = by_date.get(iso)
        if row is None:
            parts.append(
                f'<tr class="gap"><td>{esc(iso)}</td>'
                '<td colspan="5">no data published for this day</td></tr>'
            )
        else:
            trials = row["scheduled"] - row["excluded"]
            title = f"{row['scheduled']} scheduled, {row['excluded']} excluded"
            parts.append(
                "<tr>"
                f"<td>{esc(iso)}</td>"
                f'<td class="num" title="{esc(title)}">{esc(trials)}</td>'
                f'<td class="num">{esc(row["vanished"])}</td>'
                f'<td class="num">{esc(row["untracked"])}</td>'
                f'<td class="num">{esc(row["cancelled"])}</td>'
                f'<td class="num">{esc(row["completed"])}</td>'
                "</tr>"
            )
        day -= dt.timedelta(days=1)
    parts.append("</tbody></table>")
    return "\n".join(parts)


def render_route(site_dir, manifest: dict, entry: dict, daily_rows: list[dict],
                 slugs: dict[str, str], position: int | None = None) -> str:
    ranked_route = position is not None
    if ranked_route:
        rank_line = f"ranked #{position} by the lower bound of the vanished rate"
    else:
        rank_line = ("not ranked — fewer than 30 trips we could judge in the "
                     "window, so no rate is claimed for this route")

    count = len(window_dates(daily_rows))
    plural = "" if count == 1 else "s"
    window_heading = f"Last {count} complete service day{plural}"

    content = load_template("route.html.tmpl", site_dir).substitute(
        route_name=esc(route_label(entry)),
        route_long=esc(entry.get("route_long_name") or ""),
        route_id=esc(entry["route_id"]),
        agency=esc(entry.get("agency_name") or "operator not named in the timetable"),
        rank_line=esc(rank_line),
        window_heading=esc(window_heading),
        trials=esc(entry["trials"]),
        scheduled=esc(entry["scheduled"]),
        excluded=esc(entry["excluded"]),
        cancelled=esc(entry["cancelled"]),
        completed=esc(entry["completed"]),
        vanished_count=esc(entry["vanished"]),
        untracked_count=esc(entry["untracked"]),
        # Below the gate the headline percentage is withheld, because the index
        # tells readers no rate is claimed for these routes. The interval stays:
        # at small n its width is the honest signal.
        vanished_rate=fmt_rate(entry["vanished_interval"]) if ranked_route else EM_DASH,
        vanished_interval=fmt_interval(entry["vanished_interval"]),
        untracked_rate=fmt_rate(entry["untracked_interval"]) if ranked_route else EM_DASH,
        untracked_interval=fmt_interval(entry["untracked_interval"]),
        daily_table=_daily_table(entry, daily_rows),
    )
    return render_page(
        site_dir,
        title=f"Route {route_label(entry)}",
        root="../",
        current="",
        generated_at=manifest.get("generated_at", ""),
        content=content,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_route_pages.py -q; python -m pytest -q`

Expected: PASS — `10 passed` for the file, full suite green with 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```
cd C:\Users\Alex\Projects\ghost-bus
git add site/route.html.tmpl publish/site.py tests/test_site_route_pages.py
git commit -m "feat(site): per-route detail pages

Shows both rates with their own intervals and never a sum, plus a day-by-day
table across the window in which a day we did not publish is a gap row carrying
no numbers at all.

Two withholdings, both to stop the page contradicting the index: below the
30-judged-trip gate the headline percentage is replaced by an em dash (the
counts and the interval remain, and the interval's width is the honest signal
at small n), and the day-by-day table carries counts only, because a single
service day is far too small a sample to put a percentage on.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 15: methodology.html and about-data.html

**Files:**
- Create: `C:\Users\Alex\Projects\ghost-bus\site\methodology.html.tmpl`
- Create: `C:\Users\Alex\Projects\ghost-bus\site\about-data.html.tmpl`
- Modify: `C:\Users\Alex\Projects\ghost-bus\publish\site.py` (append after `render_route`)
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_site_prose_pages.py`

**Interfaces:**
- Consumes: `render_page`, `esc`, `EM_DASH`, and the manifest keys `schema_version`, `generated_at`, `timetable_hash`, `timetable_loaded_at`, `coverage.{first_day,last_day,complete_days}`, `counts.{observations,snapshots,trips_classified}`, `unnamed_routes`. `timetable_loaded_at` is emitted by `publish/dataset.py` (Task 8); this page falls back to an em dash if it is absent so an older dataset degrades visibly rather than crashing the build.
- Produces: `render_methodology(site_dir, manifest) -> str`, `render_about_data(site_dir, manifest, data_dir) -> str`.

The methodology prose below is the finished copy. Do not paraphrase it, shorten it, or "improve" it while implementing — it is the project's public defence of its own numbers and every clause is load-bearing. The claim test lowercases both sides, so capitalisation in the prose is free but wording is not.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_site_prose_pages.py`:

```python
from tests.site_fixtures import DEFAULT_MANIFEST, daily_row, uptime_row, write_dataset

from publish.site import SITE_DIR, render_about_data, render_methodology

REQUIRED_METHODOLOGY_CLAIMS = [
    "COMPLETED", "VANISHED", "UNTRACKED", "CANCELLED", "EXCLUDED",
    "we could not see it",
    "did not run",
    "our downtime",
    "never counts against",
    "one direction",
    "can hide a ghost",
    "never invent one",
    "benefit of the doubt",
    "staleness",
    "not yet acted on",
    "Wilson",
    "lower bound",
    "overlap",
    "never add",
    "30 trips we could judge",
    "14 complete days",
]


def test_methodology_makes_every_required_statement():
    html = render_methodology(SITE_DIR, DEFAULT_MANIFEST).lower()
    missing = [c for c in REQUIRED_METHODOLOGY_CLAIMS if c.lower() not in html]
    assert missing == []


def test_methodology_never_presents_a_combined_rate():
    html = render_methodology(SITE_DIR, DEFAULT_MANIFEST)
    assert "ghost rate" not in html.lower()
    assert "combined rate" not in html.lower()


def test_methodology_gate_copy_matches_the_code():
    # The gate counts trips judged, not trips scheduled. The page must not
    # claim a rule the builder does not enforce.
    html = render_methodology(SITE_DIR, DEFAULT_MANIFEST)
    assert "30 scheduled trips" not in html


def test_methodology_is_a_complete_page():
    html = render_methodology(SITE_DIR, DEFAULT_MANIFEST)
    assert html.startswith("<!doctype html>")
    assert "<script" not in html.lower()


def test_about_data_reports_the_manifest_facts(tmp_path):
    data = write_dataset(
        tmp_path / "data",
        daily_rows=[daily_row("2026-06-28", "R1", scheduled=1)],
        uptime_rows=[uptime_row("2026-06-28")],
    )
    html = render_about_data(SITE_DIR, DEFAULT_MANIFEST, data)
    assert "0f1c9a2b3d4e5f60" in html
    assert "2026-07-01T02:00:00+00:00" in html
    assert "2026-06-01" in html and "2026-06-28" in html
    assert "128400" in html or "128,400" in html
    assert "40320" in html or "40,320" in html
    assert "9111" in html or "9,111" in html
    assert "Schema version" in html and ">1<" in html


def test_about_data_links_every_csv(tmp_path):
    data = write_dataset(
        tmp_path / "data",
        daily_rows=[daily_row("2026-06-27", "R1", scheduled=1),
                    daily_row("2026-06-28", "R1", scheduled=1)],
        uptime_rows=[uptime_row("2026-06-28")],
    )
    html = render_about_data(SITE_DIR, DEFAULT_MANIFEST, data)
    assert 'href="data/daily/2026-06-27.csv"' in html
    assert 'href="data/daily/2026-06-28.csv"' in html
    assert 'href="data/uptime/2026-06-28.csv"' in html
    assert 'href="data/manifest.json"' in html


def test_about_data_lists_unnamed_routes(tmp_path):
    data = write_dataset(tmp_path / "data")
    manifest = dict(DEFAULT_MANIFEST)
    manifest["unnamed_routes"] = ["03C 120 e a", "ZZ 9"]
    html = render_about_data(SITE_DIR, manifest, data)
    assert "03C 120 e a" in html
    assert "ZZ 9" in html


def test_about_data_says_none_when_no_unnamed_routes(tmp_path):
    data = write_dataset(tmp_path / "data")
    html = render_about_data(SITE_DIR, DEFAULT_MANIFEST, data)
    assert "None" in html


def test_about_data_carries_tfi_nta_attribution(tmp_path):
    data = write_dataset(tmp_path / "data")
    html = render_about_data(SITE_DIR, DEFAULT_MANIFEST, data)
    assert "Transport for Ireland" in html
    assert "National Transport Authority" in html


def test_about_data_missing_load_date_degrades_to_em_dash(tmp_path):
    data = write_dataset(tmp_path / "data")
    manifest = {k: v for k, v in DEFAULT_MANIFEST.items() if k != "timetable_loaded_at"}
    html = render_about_data(SITE_DIR, manifest, data)
    assert "Timetable loaded</dt><dd>\u2014</dd>" in html


def test_about_data_null_coverage_renders_em_dashes_not_blanks(tmp_path):
    # An empty database publishes coverage nulls. An absent value must be shown
    # as explicitly unknown, not as an empty gap in a sentence.
    data = write_dataset(tmp_path / "data")
    manifest = dict(DEFAULT_MANIFEST)
    manifest["coverage"] = {"first_day": None, "last_day": None, "complete_days": 0}
    html = render_about_data(SITE_DIR, manifest, data)
    assert "\u2014 to \u2014" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_prose_pages.py -q`

Expected: FAIL with `ImportError: cannot import name 'render_methodology' from 'publish.site'`.

- [ ] **Step 3: Write minimal implementation**

Create `C:\Users\Alex\Projects\ghost-bus\site\methodology.html.tmpl` (verbatim — this file has no `${...}` placeholders and must not acquire any):

```html
<h1>How these numbers are made</h1>
<p class="lede">This page is the argument behind every figure on this site. If you think a number here is unfair, this is the page to attack.</p>

<h2>The five outcomes</h2>
<p>We take the operator's own published timetable, and for every scheduled trip we assign exactly one outcome. There is no “other” bucket, and nothing is quietly dropped.</p>
<dl class="facts">
<dt>COMPLETED</dt><dd>A vehicle was seen running this trip, and was still being seen near where the trip was supposed to end.</dd>
<dt>VANISHED</dt><dd>A vehicle was seen running this trip and then stopped reporting before the trip was over. This is direct evidence of a trip that did not finish as scheduled.</dd>
<dt>UNTRACKED</dt><dd>No vehicle was ever seen on this trip while we were watching.</dd>
<dt>CANCELLED</dt><dd>The operator's own real-time feed told us the trip was cancelled. We take their word for it.</dd>
<dt>EXCLUDED</dt><dd>We were not watching for enough of the trip to judge it. Our problem, not theirs.</dd>
</dl>
<p>These add up, always: excluded plus cancelled plus completed plus vanished plus untracked equals the number of scheduled trips, for every route and every day. An automated check enforces that before anything is published, and if it fails we publish nothing at all rather than publish something that does not add up.</p>

<h2>UNTRACKED means we could not see it. It does not mean it did not run.</h2>
<p>This is the most important sentence on the site.</p>
<p>A bus with a broken transmitter, a vehicle swapped in at short notice without the right equipment, a gap in the operator's real-time feed, an outage at our end that we failed to detect — every one of those looks exactly like a bus that never came. We cannot tell them apart, and we do not pretend we can. An untracked trip is reported as untracked and as nothing else.</p>
<p>If you want the number that is direct evidence of a trip failing, that is the vanished rate: a vehicle was there, and then it was not. That is the number we rank on.</p>

<h2>Why there are two rates and never one</h2>
<p>It would be easy to add the vanished rate and the untracked rate together and call the total “ghost trips”. It would also be dishonest, because it would assert precisely the thing we just said we cannot know. So we never add them. They are computed separately, published in separate columns of the dataset, displayed in separate columns of the table, and no code in this project sums them — there is a test whose only job is to fail if that ever changes.</p>
<p>The untracked rate is worth showing anyway. It tells you how much of a route's service is invisible to us, which is useful context and is sometimes itself a story about an operator's equipment. But it never moves a route up or down the table, because ranking on it would rank operators by how good their telematics is, not by whether your bus came.</p>

<h2>EXCLUDED is our downtime, and it never counts against the operator</h2>
<p>When our tracker is down, restarting, or otherwise not watching, we cannot judge the trips that were running at the time. Those trips are marked EXCLUDED and removed from the denominator of both rates. A route is never punished for the minutes we were not looking.</p>
<p>Our own uptime is published on the front page, from the first day of tracking, precisely so you can see how often that happens and hold us to it.</p>

<h2>How we decide a vehicle finished the trip</h2>
<p>Nobody tells us a trip completed. We infer it geographically: we watch where the vehicle assigned to a trip reports itself, and if the last position we have for it is within a set distance of where the trip was supposed to end, we call it completed. If we saw the vehicle earlier in the trip but it stopped reporting well short of the end, we call it vanished.</p>
<p>That method has a known error, and the error runs in one direction only. Matching is generous: a vehicle that stops reporting somewhere that happens to be close to the end of the route can be recorded as completed when it really vanished. But the reverse cannot happen — a vehicle that is genuinely reporting all the way to the end of the route cannot be mistaken for one that disappeared.</p>
<p>So a generous match can hide a ghost. It can never invent one. Read the vanished rate as a floor: the real figure is at least this high, possibly higher, never lower. Every ambiguity in our method makes the operator look better, not worse.</p>

<h2>Benefit of the doubt</h2>
<p>Where the geographic test cannot resolve a trip either way, we resolve it in the operator's favour. If we cannot show that a trip failed, we do not say it failed. We would rather understate a problem than manufacture one — a league table of bus routes is an accusation, and an accusation has to clear a higher bar than a hunch.</p>

<h2>Feed staleness: measured, not yet acted on</h2>
<p>Real-time positions arrive with a timestamp, and that timestamp is sometimes older than the moment we receive it. We record how stale every position was. Today that measurement changes nothing: no trip is classified differently because its data was stale, and no route is penalised for it. Choosing a staleness threshold requires a baseline we do not yet have, and picking a number before we have the evidence would be exactly the kind of guessing this project exists to avoid. When we do set one, it will be written down here, with the reasoning, before it affects a single published figure.</p>

<h2>How to read a confidence interval</h2>
<p>A route with 30 trips and 2 vanished shows 6.7%. But 30 trips is a small sample. Watch the same route for another month and you might see 1, or 5, without anything about the service having changed. The headline percentage alone invites you to over-read it.</p>
<p>So every rate we publish carries a 95% confidence interval: the range the true rate plausibly sits in, given how much data we actually have. We use the Wilson score interval rather than the textbook one, because the rates here sit close to zero on small samples, and the textbook formula produces nonsense there — negative lower bounds, and intervals of zero width when nothing has gone wrong yet.</p>
<p>Two rules for reading them:</p>
<ul>
<li>If two routes' intervals <strong>overlap</strong>, the data does not distinguish them, whatever their headline percentages say. Do not read a difference into it.</li>
<li>A <strong>wide</strong> interval means “we have few trips”, not “this route is wildly unreliable”. Width is about sample size, not about severity.</li>
</ul>

<h2>Why the table is ordered the way it is</h2>
<p>We rank by the <strong>lower bound</strong> of the vanished interval, worst first — not by the headline rate. A route is placed above another only where the evidence supports that ordering, so an unlucky handful of trips on a quiet route cannot leap it to the top of the table. The effect is that the table is conservative by construction: to be ranked badly here, a route has to have both a bad rate and enough trips to be sure of it.</p>

<h2>When we stay quiet</h2>
<ul>
<li><strong>Fewer than 30 trips we could judge</strong> in the 28-day window: the route is listed under “not enough data yet” with its counts visible, is never ranked, and no headline rate is claimed for it. The count that matters is trips we could actually judge — scheduled trips minus the ones we were not watching — because a route with thirty scheduled trips and twenty-nine we missed has told us almost nothing, and shaming it on the one trip we saw would be indefensible.</li>
<li><strong>Fewer than 14 complete days</strong> of tracking: no route-level number is published at all, in the dataset or on the site. Only our own uptime, which is a claim about us and not about anyone else.</li>
<li><strong>Only complete service days count.</strong> Today is never in the table: a partial day understates trip counts and distorts every rate computed from it.</li>
<li><strong>Missing days are shown as gaps</strong> and never interpolated. If we did not publish a day, you will see a hole, not a guess.</li>
<li><strong>A rate with no trips behind it shows as “—”</strong>, never as 0.0%. An undefined rate is not a perfect score.</li>
</ul>

<h2>Check it yourself</h2>
<p>The site you are reading is built from the published CSV files and nothing else — it has no access to our database, so a number here cannot differ from the number in the data you can download. Take the files, recompute, and tell us if you get something different.</p>
<p class="note"><a href="about-data.html">Download the data</a> · <a href="index.html">Back to the scoreboard</a></p>
```

Create `C:\Users\Alex\Projects\ghost-bus\site\about-data.html.tmpl`:

```html
<h1>About the data</h1>
<p class="lede">Everything on this site is computed from the files linked below. Nothing else feeds the pages you are reading.</p>

<h2>This release</h2>
<dl class="facts">
<dt>Schema version</dt><dd>${schema_version}</dd>
<dt>Generated at</dt><dd>${generated_at}</dd>
<dt>Timetable hash</dt><dd><code>${timetable_hash}</code></dd>
<dt>Timetable loaded</dt><dd>${timetable_loaded}</dd>
<dt>Coverage</dt><dd>${coverage_first} to ${coverage_last} — ${complete_days} complete service days</dd>
<dt>Real-time snapshots collected</dt><dd>${snapshots}</dd>
<dt>Vehicle observations recorded</dt><dd>${observations}</dd>
<dt>Trips classified</dt><dd>${trips_classified}</dd>
<dt>Routes present in the outcomes but absent from the timetable's route table</dt><dd>${unnamed_routes}</dd>
</dl>
<p class="note">Routes missing from the timetable's route table are shown by their raw route id rather than a name. We list them here rather than dropping them, because a route we cannot name is still a route we counted.</p>

<h2>Files</h2>
<p>Every figure on this site is recomputable from these. The manifest is the machine-readable version of the facts above.</p>
${csv_links}

<h2>Attribution and licence</h2>
<p>Timetable data and real-time vehicle positions come from the General Transit Feed Specification feeds published by <strong>Transport for Ireland</strong> / the <strong>National Transport Authority</strong>. Ghost Bus is an independent project and is not affiliated with, endorsed by, or operated by TFI, the NTA, Dublin Bus, Go-Ahead Ireland, or any other operator.</p>
<p>The classifications, rates and intervals in these files are ours, not theirs. If you use them, say so, and link to the methodology so that whoever reads your work can see what the numbers do and do not claim.</p>
<p class="note"><a href="methodology.html">Methodology</a> · <a href="index.html">Back to the scoreboard</a></p>
```

Append to the end of `publish/site.py`:

```python
def render_methodology(site_dir, manifest: dict) -> str:
    content = load_template("methodology.html.tmpl", site_dir).substitute()
    return render_page(
        site_dir,
        title="Methodology",
        root="",
        current="methodology.html",
        generated_at=manifest.get("generated_at", ""),
        content=content,
    )


def _csv_links(data_dir) -> str:
    data_dir = Path(data_dir)
    parts = ['<ul class="files">']
    parts.append('<li><a href="data/manifest.json">manifest.json</a> — this release, machine readable</li>')
    for label, sub in (("Daily route outcomes", "daily"), ("Tracker uptime", "uptime")):
        directory = data_dir / sub
        files = sorted(directory.glob("*.csv")) if directory.is_dir() else []
        if not files:
            parts.append(f"<li>{esc(label)}: none published yet</li>")
            continue
        parts.append(f"<li>{esc(label)}:<ul>")
        for path in files:
            href = f"data/{sub}/{path.name}"
            parts.append(f'<li><a href="{esc(href)}">{esc(path.name)}</a></li>')
        parts.append("</ul></li>")
    parts.append("</ul>")
    return "\n".join(parts)


def render_about_data(site_dir, manifest: dict, data_dir) -> str:
    coverage = manifest.get("coverage") or {}
    counts = manifest.get("counts") or {}
    unnamed = manifest.get("unnamed_routes") or []
    unnamed_html = (
        "None"
        if not unnamed
        else ", ".join(f"<code>{esc(route_id)}</code>" for route_id in unnamed)
    )

    def shown(value) -> str:
        # `or EM_DASH`, not a .get default: the manifest publishes JSON nulls
        # for an empty database, and an absent value must read as explicitly
        # unknown rather than as a blank gap in a sentence.
        return esc(value) if value else EM_DASH

    content = load_template("about-data.html.tmpl", site_dir).substitute(
        schema_version=esc(manifest.get("schema_version", "")),
        generated_at=esc(manifest.get("generated_at", "")),
        timetable_hash=esc(manifest.get("timetable_hash", "")),
        timetable_loaded=shown(manifest.get("timetable_loaded_at")),
        coverage_first=shown(coverage.get("first_day")),
        coverage_last=shown(coverage.get("last_day")),
        complete_days=esc(coverage.get("complete_days", 0)),
        snapshots=esc(counts.get("snapshots", 0)),
        observations=esc(counts.get("observations", 0)),
        trips_classified=esc(counts.get("trips_classified", 0)),
        unnamed_routes=unnamed_html,
        csv_links=_csv_links(data_dir),
    )
    return render_page(
        site_dir,
        title="About the data",
        root="",
        current="about-data.html",
        generated_at=manifest.get("generated_at", ""),
        content=content,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_prose_pages.py -q; python -m pytest -q`

Expected: PASS — `11 passed` for the file, full suite green with 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```powershell
cd C:\Users\Alex\Projects\ghost-bus
git add site/methodology.html.tmpl site/about-data.html.tmpl publish/site.py tests/test_site_prose_pages.py
git commit -m @'
feat(site): methodology and about-data pages

Methodology states the five outcomes, that UNTRACKED means we could not see it
rather than that it did not run, that EXCLUDED is our downtime, the one
directional error in the geographic matching, the benefit-of-the-doubt rule,
that staleness is measured but not acted on, and how to read the intervals. A
test asserts every one of those claims is present so the page cannot be quietly
hollowed out, and that the stated 30-trip gate matches the one the code
enforces.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
'@
```

---

### Task 16: `build_site()` end to end, CLI entry point, and the golden HTML test

**Files:**
- Modify: `C:\Users\Alex\Projects\ghost-bus\publish\site.py` (extend the import block; append after `render_about_data`)
- Create: `C:\Users\Alex\Projects\ghost-bus\tests\golden\index_board.html` (generated in Step 4, then reviewed and committed)
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_site_build.py`

**Interfaces:**
- Consumes: every renderer from Tasks 10–15; `route_slugs` from the published manifest (Task 8); `publish.slugs.slug_map` (Task 7).
- Produces: `DatasetError(RuntimeError)`, `build_site(data_dir, out_dir, site_dir=SITE_DIR) -> dict` (returns the manifest it wrote) and `main(argv=None) -> int`, invoked as `python -m publish.site --data data --out _site`.

`build_site` writes `<out>/index.html`, `<out>/methodology.html`, `<out>/about-data.html`, `<out>/style.css`, `<out>/route/<slug>.html` for every route (only when `scoreboard_ready`), an **allowlisted** copy of the dataset at `<out>/data/` so every CSV linked from the about page resolves, and `<out>/manifest.json`, so the slug for any route id is auditable from the site itself.

**Route URLs come from the dataset, and the builder never reads its own output.** `route_slugs` is written by the VM into `data/manifest.json` (Task 8) and checked out here with the data. The builder takes that map as given; it computes a slug only for a route id the map does not carry, which should not happen — the publisher assigns one to every route it publishes — and is a fallback rather than a `KeyError` mid-build. Reading the map back out of `<out>/manifest.json` would be worthless: CI checks out into a brand-new `_site` on an ephemeral runner every run (D4, Task 19), so that file never exists when the build starts, and the stable-URL guarantee would never engage in production even though a test reusing one output directory inside a single process would pass.

Two fail-closed rules, both tested. **Allowlisted copy:** only `manifest.json` and `daily|uptime/YYYY-MM-DD.csv` are copied; anything else under `data/` aborts the build. A blanket `copytree` would serve an attacker-written `data/x.html` from the site's own origin, riding the legitimate token through the legitimate workflow. **Gate consistency:** if `scoreboard_ready` is false while `data/daily/` exists, the build refuses rather than rendering "we publish nothing about any route" on a page that links route data.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_site_build.py`:

```python
import json
import os
from pathlib import Path

import pytest

from tests.site_fixtures import daily_row, uptime_row, write_dataset

from publish.site import (DatasetError, build_site, leaderboard, read_daily,
                          render_board)
from publish.slugs import slug_map

GOLDEN = Path(__file__).parent / "golden" / "index_board.html"


def day_rows(route_id, scheduled, vanished=0, untracked=0, excluded=0,
             day="2026-06-28", **kw):
    return daily_row(day, route_id, scheduled=scheduled, excluded=excluded,
                     vanished=vanished, untracked=untracked, cancelled=0,
                     completed=scheduled - excluded - vanished - untracked, **kw)


def ready_dataset(tmp_path):
    daily = [
        day_rows("BIG", 200, vanished=8, untracked=4,
                 route_short_name="1", route_long_name="Fixtureville Main",
                 agency_name="Fixtureville Bus"),
        day_rows("SMALL", 30, vanished=2, untracked=1,
                 route_short_name="2", route_long_name="Fixtureville Orbital",
                 agency_name="Fixtureville Bus"),
        day_rows("03C 120 e a", 12, vanished=1,
                 route_short_name="120", route_long_name="Fixtureville Crosstown",
                 agency_name="Go-Ahead Fixtureville"),
    ]
    uptime = [uptime_row("2026-06-28", 1440, 1440), uptime_row("2026-06-26", 1440, 1200)]
    return write_dataset(tmp_path / "data", daily_rows=daily, uptime_rows=uptime,
                         manifest={"coverage": {"first_day": "2026-06-28",
                                                "last_day": "2026-06-28",
                                                "complete_days": 28}})


def test_build_site_writes_every_page(tmp_path):
    data = ready_dataset(tmp_path)
    out = tmp_path / "_site"
    build_site(data, out)
    for name in ("index.html", "methodology.html", "about-data.html", "style.css",
                 "manifest.json"):
        assert (out / name).is_file(), name
    assert (out / "route" / "big.html").is_file()
    assert (out / "route" / "small.html").is_file()
    assert (out / "route" / "03c-120-e-a.html").is_file()


def test_build_site_copies_the_data_tree_so_csv_links_resolve(tmp_path):
    data = ready_dataset(tmp_path)
    out = tmp_path / "_site"
    build_site(data, out)
    assert (out / "data" / "daily" / "2026-06-28.csv").is_file()
    assert (out / "data" / "uptime" / "2026-06-28.csv").is_file()
    about = (out / "about-data.html").read_text(encoding="utf-8")
    for href in ("data/daily/2026-06-28.csv", "data/uptime/2026-06-28.csv"):
        assert f'href="{href}"' in about
        assert (out / href).is_file()


def test_build_site_refuses_an_unexpected_file_in_the_dataset(tmp_path):
    # A blanket copy would serve attacker-written HTML from this site's own
    # origin, riding the legitimate token through the legitimate workflow.
    data = ready_dataset(tmp_path)
    (data / "evil.html").write_text("<p>x</p>", encoding="utf-8")
    with pytest.raises(DatasetError):
        build_site(data, tmp_path / "_site")


def test_build_site_refuses_a_non_csv_under_daily(tmp_path):
    data = ready_dataset(tmp_path)
    (data / "daily" / "evil.html").write_text("<p>x</p>", encoding="utf-8")
    with pytest.raises(DatasetError):
        build_site(data, tmp_path / "_site")


def test_build_site_refuses_route_data_behind_a_pre_baseline_page(tmp_path):
    data = ready_dataset(tmp_path)
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    manifest["scoreboard_ready"] = False
    (data / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with pytest.raises(DatasetError):
        build_site(data, tmp_path / "_site")


def test_build_site_records_the_slug_map_in_the_manifest(tmp_path):
    data = ready_dataset(tmp_path)
    out = tmp_path / "_site"
    written = build_site(data, out)
    on_disk = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk == written
    assert on_disk["route_slugs"]["03C 120 e a"] == "03c-120-e-a"
    assert on_disk["schema_version"] == 1


def incumbent_dataset(tmp_path):
    """A dataset whose manifest already publishes a slug for an incumbent route.

    "03C 120 e a" sorts before "03C/120/e/a" (0x20 < 0x2F), so a fresh
    assignment would give the bare slug to the newcomer and move the
    incumbent's live URL. The published map is what stops that.
    """
    rows = read_daily(ready_dataset(tmp_path)) + [
        day_rows("03C/120/e/a", 40, vanished=2, route_short_name="120x")]
    return write_dataset(
        tmp_path / "data", daily_rows=rows,
        uptime_rows=[uptime_row("2026-06-28", 1440, 1440)],
        manifest={"coverage": {"first_day": "2026-06-28",
                               "last_day": "2026-06-28", "complete_days": 28},
                  "route_slugs": {"03C/120/e/a": "03c-120-e-a"}})


def test_build_site_honours_the_slug_map_published_in_the_dataset(tmp_path):
    """The dataset decides route URLs; the builder obeys the map it is given."""
    data = incumbent_dataset(tmp_path)
    out = tmp_path / "_site"
    written = build_site(data, out)
    assert written["route_slugs"]["03C/120/e/a"] == "03c-120-e-a"
    assert written["route_slugs"]["03C 120 e a"] == "03c-120-e-a-2"
    assert (out / "route" / "03c-120-e-a.html").is_file()
    assert (out / "route" / "03c-120-e-a-2.html").is_file()


def test_route_urls_are_identical_across_two_fresh_output_directories(tmp_path):
    """Models the ephemeral CI runner, where _site never survives a run.

    The stable-URL guarantee has to come from the dataset, which is checked
    out, and not from the previous build, which on a fresh runner does not
    exist. Rebuilding into ONE reused out_dir would pass even if the map were
    read back out of the site's own output - that is exactly the bug this pins,
    so the two builds must go to two different, previously nonexistent dirs.
    """
    data = incumbent_dataset(tmp_path)

    def route_files(out):
        return sorted(p.name for p in (out / "route").iterdir())

    first = build_site(data, tmp_path / "run-1")
    second = build_site(data, tmp_path / "run-2")

    assert first["route_slugs"] == second["route_slugs"]
    assert route_files(tmp_path / "run-1") == route_files(tmp_path / "run-2")
    assert "03c-120-e-a.html" in route_files(tmp_path / "run-1")


def test_build_site_is_idempotent(tmp_path):
    data = ready_dataset(tmp_path)
    out = tmp_path / "_site"
    build_site(data, out)
    first = (out / "index.html").read_text(encoding="utf-8")
    build_site(data, out)
    assert (out / "index.html").read_text(encoding="utf-8") == first


def test_pre_baseline_build_emits_no_route_pages(tmp_path):
    data = write_dataset(
        tmp_path / "data",
        uptime_rows=[uptime_row("2026-06-09", 1440, 1440)],
        manifest={"scoreboard_ready": False,
                  "coverage": {"first_day": "2026-06-01", "last_day": "2026-06-09",
                               "complete_days": 9}},
    )
    out = tmp_path / "_site"
    build_site(data, out)
    assert not (out / "route").exists()
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "<table" not in index
    assert "day 9 of 14" in index
    assert (out / "methodology.html").is_file()
    assert (out / "about-data.html").is_file()
    assert "uptime-strip" in index


def test_output_files_are_utf8(tmp_path):
    data = ready_dataset(tmp_path)
    out = tmp_path / "_site"
    build_site(data, out)
    text = (out / "index.html").read_bytes().decode("utf-8")
    assert "\u2014" in text or "\u2013" in text


def test_leaderboard_html_matches_the_golden(tmp_path):
    """Golden HTML for the board fragment.

    To regenerate after an intentional markup change:
        GHOSTBUS_UPDATE_GOLDEN=1 python -m pytest tests/test_site_build.py -q
    then read the diff before committing it.
    """
    data = ready_dataset(tmp_path)
    rows = read_daily(data)
    ranked, unranked = leaderboard(rows)
    slugs = slug_map(e["route_id"] for e in ranked + unranked)
    got = render_board(ranked, unranked, slugs)

    if os.environ.get("GHOSTBUS_UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(got, encoding="utf-8")

    assert GOLDEN.is_file(), "golden missing; regenerate with GHOSTBUS_UPDATE_GOLDEN=1"
    assert got == GOLDEN.read_text(encoding="utf-8")


def test_golden_pins_the_facts_that_matter(tmp_path):
    """Belt and braces: the golden could be regenerated wrong, these cannot."""
    data = ready_dataset(tmp_path)
    rows = read_daily(data)
    ranked, unranked = leaderboard(rows)
    slugs = slug_map(e["route_id"] for e in ranked + unranked)
    got = render_board(ranked, unranked, slugs)

    assert got.index(">1<") < got.index(">2<")          # BIG ranked above SMALL
    assert "Not enough data yet" in got
    assert got.index("Not enough data yet") < got.index("120")
    assert "4.0%" in got and "6.7%" in got              # both point estimates shown
    # Per-row sums that must never appear: BIG 4.0+2.0, SMALL 6.7+3.3.
    assert "6.0%" not in got
    assert "10.0%" not in got
    assert 'href="route/03c-120-e-a.html"' in got
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_build.py -q`

Expected: FAIL with `ImportError: cannot import name 'DatasetError' from 'publish.site'`.

- [ ] **Step 3: Write minimal implementation**

Add to the import block of `publish/site.py`, after `import datetime as dt`:

```python
import argparse
import shutil
import sys
```

Append to the end of `publish/site.py`:

```python
class DatasetError(RuntimeError):
    """The published dataset is not shaped the way publish/dataset.py writes it."""


_DATA_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.csv$")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _copy_dataset(data_dir: Path, dest: Path) -> None:
    """Copy only files whose shape publish/dataset.py produces.

    An unexpected path under data/ means something other than the publisher
    wrote there, and we refuse to serve it rather than guess it is harmless: a
    blanket copy would put attacker-authored HTML on this site's own origin,
    carried by the legitimate token through the legitimate workflow.
    """
    allowed = set()
    manifest = data_dir / "manifest.json"
    if manifest.is_file():
        allowed.add(manifest)
    for sub in ("daily", "uptime"):
        directory = data_dir / sub
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if not (path.is_file() and _DATA_FILE.match(path.name)):
                raise DatasetError(f"unexpected entry in dataset: {path}")
            allowed.add(path)
    for path in data_dir.rglob("*"):
        if path.is_file() and path not in allowed:
            raise DatasetError(f"unexpected file in dataset: {path}")
    if dest.exists():
        shutil.rmtree(dest)
    for path in sorted(allowed):
        target = dest / path.relative_to(data_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def build_site(data_dir, out_dir, site_dir=SITE_DIR) -> dict:
    """Render the whole site from the published dataset into out_dir.

    Returns the manifest as written to out_dir, so the id-to-filename mapping
    is auditable from the site as well as from the dataset.

    Route URLs come from the dataset's own route_slugs map and never from a
    previous build: CI checks the dataset out beside the code and renders into
    a brand-new _site every run, so out_dir is always empty when we start.
    """
    data_dir = Path(data_dir)
    out_dir = Path(out_dir)
    site_dir = Path(site_dir)

    manifest = read_manifest(data_dir)
    daily_rows = read_daily(data_dir)
    uptime_rows = read_uptime(data_dir)
    ready = bool(manifest.get("scoreboard_ready"))

    if not ready and (data_dir / "daily").is_dir():
        raise DatasetError(
            "scoreboard_ready is false but data/daily exists - refusing to "
            "publish route data behind a page that says we publish none")

    ranked: list[dict] = []
    unranked: list[dict] = []
    slugs: dict[str, str] = {}
    if ready:
        ranked, unranked = leaderboard(daily_rows)
        # The publisher assigns a slug to every route it publishes, so the map
        # should already cover all of these; slug_map fills in anything missing
        # rather than raising mid-build, and never moves a published entry.
        slugs = slug_map((entry["route_id"] for entry in ranked + unranked),
                         existing=manifest.get("route_slugs") or {})

    out_dir.mkdir(parents=True, exist_ok=True)
    pages = {
        "index.html": render_index(site_dir, manifest, daily_rows, uptime_rows,
                                   ranked, unranked, slugs),
        "methodology.html": render_methodology(site_dir, manifest),
        "about-data.html": render_about_data(site_dir, manifest, data_dir),
    }
    for name, text in pages.items():
        _write(out_dir / name, text)
    shutil.copyfile(site_dir / "style.css", out_dir / "style.css")

    route_dir = out_dir / "route"
    if route_dir.exists():
        shutil.rmtree(route_dir)
    if ready:
        for position, entry in enumerate(ranked, start=1):
            name = f"{slugs[entry['route_id']]}.html"
            _write(route_dir / name,
                   render_route(site_dir, manifest, entry, daily_rows, slugs, position))
        for entry in unranked:
            name = f"{slugs[entry['route_id']]}.html"
            _write(route_dir / name,
                   render_route(site_dir, manifest, entry, daily_rows, slugs, None))

    _copy_dataset(data_dir, out_dir / "data")

    written = dict(manifest)
    # The slugs of the pages this build actually emitted. The dataset's own
    # manifest - copied verbatim to <out>/data/manifest.json - remains the
    # authority, and carries entries for withdrawn routes too.
    written["route_slugs"] = slugs
    _write(out_dir / "manifest.json", json.dumps(written, indent=2, sort_keys=True) + "\n")
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the Ghost Bus site from published CSVs.")
    parser.add_argument("--data", default="data", help="published dataset directory")
    parser.add_argument("--out", default="_site", help="output directory")
    parser.add_argument("--site", default=str(SITE_DIR), help="template directory")
    args = parser.parse_args(argv)
    manifest = build_site(args.data, args.out, args.site)
    routes = len(manifest.get("route_slugs") or {})
    ready = manifest.get("scoreboard_ready")
    print(f"built {args.out}: scoreboard_ready={ready}, {routes} route pages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Generate the golden once, read it, then run clean:

```powershell
cd C:\Users\Alex\Projects\ghost-bus
$env:GHOSTBUS_UPDATE_GOLDEN = "1"; python -m pytest tests/test_site_build.py -q; Remove-Item Env:\GHOSTBUS_UPDATE_GOLDEN
```

Open `tests/golden/index_board.html` and confirm by eye: BIG is row 1 and SMALL row 2 (the lower-bound ordering), `4.0%` and `6.7%` both appear, the `03C 120 e a` route is under "Not enough data yet" with its counts, and no cell shows a per-row sum of the two rates (`6.0%` or `10.0%`). Then:

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_build.py -q; python -m pytest -q`

Expected: PASS — `13 passed` for the file, full suite green with 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```powershell
cd C:\Users\Alex\Projects\ghost-bus
git add publish/site.py tests/test_site_build.py tests/golden/index_board.html
git commit -m @'
feat(site): build_site end to end plus CLI entry point

Renders index, methodology, about-data and one page per route from the
published CSVs, copies only the dataset files the publisher produces (an
unexpected path aborts the build rather than serving it from our own origin),
and writes a manifest recording the slug of every page it emitted. Refuses to
build route data behind a pre-baseline page.

Route URLs come from the dataset's route_slugs map, never from a previous
build: CI renders into a brand-new _site on an ephemeral runner every run, so a
map read back from the site output would always be empty and published URLs
would move. Pinned by a test that builds twice into two different fresh output
directories and compares the URLs.

Golden HTML for the board fragment, backed by hard assertions so a
mis-generated golden cannot pass silently.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
'@
```

---

### Task 17: SECURITY — escaping is enforced, and the build refuses to emit live markup

**Files:**
- Modify: `C:\Users\Alex\Projects\ghost-bus\publish\site.py` (append `InjectionError`/`assert_inert` after `_write`; add calls inside `build_site`)
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_site_escaping.py`

**Interfaces:**
- Consumes: `build_site` (Task 16), `esc`, `publish.slugs.slugify` (Task 7).
- Produces: `class InjectionError(RuntimeError)`, `ALLOWED_TAGS: frozenset[str]`, `assert_inert(text: str, source: str = "") -> None`.

Escaping is invisible when it breaks — a missing `esc()` produces a page that looks fine until someone hostile is in the timetable. So there are two independent guards. **Structural:** every emitted HTML file is parsed for tag names and any tag outside a fixed allowlist aborts the build; attribute-level checks are deliberately avoided, because a route legitimately named `x onerror=y` must not fail the build and, once escaped, cannot do anything. **Field-agnostic:** a single test asserts no hostile payload appears verbatim in any emitted file, which catches a missed `esc()` on *any* field including attribute-context payloads containing no `<`, which the structural guard cannot see. The hostile fixture covers the ranked, unranked and zero-trial render paths.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_site_escaping.py`:

```python
"""Security test for D5: externally-sourced strings must render inert.

Route names come from the GTFS feed. We do not control them. This file is the
pin that says so.
"""
import pytest

from tests.site_fixtures import daily_row, uptime_row, write_dataset

from publish.site import InjectionError, assert_inert, build_site
from publish.slugs import slugify

XSS_ID = "<script>alert(1)</script>"
XSS_SHORT = '" onmouseover="alert(2)'
XSS_LONG = "<img src=x onerror=alert(3)>"
XSS_AGENCY = "</table><script src='//evil.example/x.js'></script>"
HOSTILE = (XSS_ID, XSS_SHORT, XSS_LONG, XSS_AGENCY)


def hostile_dataset(tmp_path):
    """Hostile names on three routes covering all three render paths: ranked,
    unranked (too few judged trips), and zero-trial (every trip excluded)."""
    def row(route_id, scheduled, excluded, vanished, untracked):
        return daily_row(
            "2026-06-28", route_id, scheduled=scheduled, excluded=excluded,
            cancelled=0,
            completed=scheduled - excluded - vanished - untracked,
            vanished=vanished, untracked=untracked,
            route_short_name=XSS_SHORT, route_long_name=XSS_LONG,
            agency_name=XSS_AGENCY)

    rows = [
        row(XSS_ID, 100, 0, 5, 1),                 # ranked
        row("HOSTILE_TINY", 12, 0, 1, 0),          # unranked
        row("HOSTILE_BLIND", 40, 40, 0, 0),        # zero trials -> em dashes
        daily_row("2026-06-28", "SAFE", scheduled=50, excluded=0, cancelled=0,
                  completed=48, vanished=1, untracked=1,
                  route_short_name="7", route_long_name="Safe Road",
                  agency_name="Fixtureville Bus"),
    ]
    return write_dataset(tmp_path / "data", daily_rows=rows,
                         uptime_rows=[uptime_row("2026-06-28")],
                         manifest={"coverage": {"first_day": "2026-06-28",
                                                "last_day": "2026-06-28",
                                                "complete_days": 28},
                                   "unnamed_routes": [XSS_ID],
                                   # Published by the VM, hostile ids and all -
                                   # the builder reads this map, so it is part
                                   # of the untrusted input surface.
                                   "route_slugs": {
                                       XSS_ID: "script-alert-1-script",
                                       "HOSTILE_TINY": "hostile-tiny",
                                       "HOSTILE_BLIND": "hostile-blind",
                                       "SAFE": "safe"}})


def all_html(out):
    return {path: path.read_text(encoding="utf-8") for path in out.rglob("*.html")}


def test_no_hostile_string_appears_verbatim_in_any_emitted_file(tmp_path):
    """Field-agnostic: catches a missed esc() on any field, on any page,
    including attribute payloads with no '<' that assert_inert cannot see."""
    out = tmp_path / "_site"
    build_site(hostile_dataset(tmp_path), out)
    pages = all_html(out)
    assert pages
    for path, text in pages.items():
        for payload in HOSTILE:
            assert payload not in text, f"{payload!r} unescaped in {path}"


def test_no_emitted_page_contains_a_script_or_img_tag(tmp_path):
    out = tmp_path / "_site"
    build_site(hostile_dataset(tmp_path), out)
    for path, text in all_html(out).items():
        assert "<script" not in text.lower(), path
        assert "<img" not in text.lower(), path


def test_the_script_route_name_appears_escaped_and_inert(tmp_path):
    out = tmp_path / "_site"
    build_site(hostile_dataset(tmp_path), out)
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in index


def test_quote_injection_cannot_break_out_of_an_attribute(tmp_path):
    out = tmp_path / "_site"
    build_site(hostile_dataset(tmp_path), out)
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "&quot; onmouseover=&quot;alert(2)" in index


def test_hostile_agency_name_is_escaped_on_the_route_page(tmp_path):
    out = tmp_path / "_site"
    build_site(hostile_dataset(tmp_path), out)
    page = (out / "route" / "script-alert-1-script.html").read_text(encoding="utf-8")
    assert "&lt;/table&gt;&lt;script" in page


def test_hostile_route_id_slugifies_to_a_safe_filename(tmp_path):
    # The map now comes from the dataset; the hostile fixture publishes it, so
    # a hostile route id has to survive the round trip through manifest.json
    # and still land on a filename made only of [a-z0-9-].
    assert slugify(XSS_ID) == "script-alert-1-script"
    out = tmp_path / "_site"
    manifest = build_site(hostile_dataset(tmp_path), out)
    assert manifest["route_slugs"][XSS_ID] == "script-alert-1-script"
    assert (out / "route" / "script-alert-1-script.html").is_file()
    for path in (out / "route").iterdir():
        assert all(c.isalnum() or c in "-." for c in path.name), path.name


def test_unnamed_routes_list_on_about_page_is_escaped(tmp_path):
    out = tmp_path / "_site"
    build_site(hostile_dataset(tmp_path), out)
    about = (out / "about-data.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in about


def test_every_emitted_page_passes_the_tag_allowlist(tmp_path):
    out = tmp_path / "_site"
    build_site(hostile_dataset(tmp_path), out)
    for path, text in all_html(out).items():
        assert_inert(text, str(path))


def test_assert_inert_rejects_an_injected_script():
    with pytest.raises(InjectionError) as excinfo:
        assert_inert("<p>ok</p><script>alert(1)</script>", "doctored.html")
    assert "script" in str(excinfo.value)
    assert "doctored.html" in str(excinfo.value)


def test_assert_inert_rejects_other_live_elements():
    for markup in ("<iframe src=x></iframe>", "<object data=x>", "<embed src=x>",
                   "<svg onload=alert(1)>", "<form action=x>",
                   "<style>x{}</style>", "<base href=x>", "<template>x</template>"):
        with pytest.raises(InjectionError):
            assert_inert(markup)


def test_assert_inert_rejects_a_javascript_href():
    with pytest.raises(InjectionError):
        assert_inert('<a href="javascript:alert(1)">x</a>')


def test_assert_inert_accepts_escaped_hostile_text():
    assert_inert("<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>")


def test_assert_inert_does_not_false_positive_on_attribute_like_text():
    assert_inert("<td>x onerror=y</td>")


def test_build_site_raises_if_a_page_would_carry_live_markup(tmp_path, monkeypatch):
    """The guard is wired into build_site, not just available to tests."""
    import publish.site as site

    monkeypatch.setattr(site, "render_methodology",
                        lambda *a, **k: "<!doctype html><html><body><script>x</script></body></html>")
    with pytest.raises(InjectionError):
        build_site(hostile_dataset(tmp_path), tmp_path / "_site")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_escaping.py -q`

Expected: FAIL with `ImportError: cannot import name 'InjectionError' from 'publish.site'`.

- [ ] **Step 3: Write minimal implementation**

Append to `publish/site.py` immediately after the `_write` function:

```python
class InjectionError(RuntimeError):
    """Raised when a rendered page contains markup we did not put there."""


ALLOWED_TAGS = frozenset({
    "html", "head", "meta", "title", "link", "body",
    "header", "nav", "main", "footer", "section",
    "h1", "h2", "h3", "p", "a", "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "th", "td",
    "dl", "dt", "dd", "span", "strong", "em", "code", "small", "abbr", "br", "hr",
})

_TAG_NAME = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)")
_JS_HREF = re.compile(r"""(?:href|src)\s*=\s*["']?\s*javascript:""", re.IGNORECASE)


def assert_inert(text: str, source: str = "") -> None:
    """Fail the build if a page carries markup we did not author.

    Externally-sourced strings (route names, agency names, route ids) are
    escaped before templating, so a hostile name reaches the page as
    &lt;script&gt; - text, with no "<" and therefore no tag. If an unescaped one
    ever slips through, it produces a real tag, that tag is not on the
    allowlist, and the build stops rather than shipping it.

    Deliberately a tag-name allowlist and not an attribute scan: a route
    legitimately named 'x onerror=y' must not fail the build, and once escaped
    it cannot do anything anyway. The field-agnostic verbatim-payload test in
    tests/test_site_escaping.py covers what this cannot see.
    """
    where = f" in {source}" if source else ""
    for name in _TAG_NAME.findall(text):
        if name.lower() not in ALLOWED_TAGS:
            raise InjectionError(f"disallowed <{name}> tag{where}")
    if _JS_HREF.search(text):
        raise InjectionError(f"javascript: URL{where}")
```

Then wire it into `build_site`. Replace:

```python
    for name, text in pages.items():
        _write(out_dir / name, text)
```

with:

```python
    for name, text in pages.items():
        assert_inert(text, name)
        _write(out_dir / name, text)
```

And replace the two route-page loops inside the `if ready:` block:

```python
        for position, entry in enumerate(ranked, start=1):
            name = f"{slugs[entry['route_id']]}.html"
            _write(route_dir / name,
                   render_route(site_dir, manifest, entry, daily_rows, slugs, position))
        for entry in unranked:
            name = f"{slugs[entry['route_id']]}.html"
            _write(route_dir / name,
                   render_route(site_dir, manifest, entry, daily_rows, slugs, None))
```

with:

```python
        for position, entry in enumerate(ranked, start=1):
            name = f"{slugs[entry['route_id']]}.html"
            text = render_route(site_dir, manifest, entry, daily_rows, slugs, position)
            assert_inert(text, f"route/{name}")
            _write(route_dir / name, text)
        for entry in unranked:
            name = f"{slugs[entry['route_id']]}.html"
            text = render_route(site_dir, manifest, entry, daily_rows, slugs, None)
            assert_inert(text, f"route/{name}")
            _write(route_dir / name, text)
```

Note for the implementer: `render_methodology` and friends are called through the module globals already, since `build_site` lives in the same module — the monkeypatch test bites without any change.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_site_escaping.py -q; python -m pytest -q`

Expected: PASS — `13 passed` for the file, full suite green with 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```powershell
cd C:\Users\Alex\Projects\ghost-bus
git add publish/site.py tests/test_site_escaping.py
git commit -m @'
feat(site): refuse to emit live markup, and pin the escaping

Route names come from GTFS, so escaping them is a security requirement (D5). A
route named <script>alert(1)</script> renders inert, slugifies to a safe
filename, and is escaped on the index, its route page and the unnamed-routes
list. Two guards: a tag allowlist over every emitted page inside build_site,
and a field-agnostic test that no hostile payload appears verbatim in any file
- which catches attribute-context payloads the allowlist cannot see. The
hostile fixture covers the ranked, unranked and zero-trial render paths.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
'@
```

---

### Task 18: VM publisher — dataset-only push to the data repository, systemd timer

**Precondition:** `python -m publish.dataset --help` must list `--db` and `--data-dir` (and no `--commit`). If it does not, Task 9 is not done — stop.

**Files:**
- Create: `C:\Users\Alex\Projects\ghost-bus\ops\publish.sh`
- Create: `C:\Users\Alex\Projects\ghost-bus\ops\git-askpass.sh`
- Create: `C:\Users\Alex\Projects\ghost-bus\ops\ghostbus-publisher.service`
- Create: `C:\Users\Alex\Projects\ghost-bus\ops\ghostbus-publisher.timer`
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_publish_ops.py`

**Interfaces:**
- Consumes: `python -m publish.dataset --db <db_path> --data-dir <data_dir>` (Task 9; exits nonzero and writes nothing when the publish gate fails).
- Consumes: the deployed layout used by `ops/ghostbus-classifier.service` — `WorkingDirectory=/opt/ghost-bus`, `EnvironmentFile=/etc/ghostbus.env`, interpreter `/opt/ghost-bus/.venv/bin/python`.
- Produces: `ops/publish.sh` — the single VM entry point that generates the dataset and pushes it. Task 19's RUNBOOK documents installing and running exactly these files.

**The dataset lives in its own repository.** `ops/publish.sh` maintains a separate checkout of `aleks-drozy/ghost-bus-data` at `/opt/ghost-bus/data-repo` and pushes there. This is what makes the split-trust claim true: a fine-grained token with `Contents: Read and write` on the *code* repo could write `publish/site.py` or any template, and CI checks the code repo out and executes it — arbitrary HTML on the site, and arbitrary code execution in CI. Scoped to a repository that contains only CSVs and a manifest, the same permission cannot reach a line of executable code.

The token never appears in a command line (`ps` on a shared box would show it), never in the repo, and never in a log line. It reaches git through `GIT_ASKPASS`, which reads it from the process environment systemd loaded from `/etc/ghostbus.env` — the same file and mode-600 pattern as `NTA_API_KEY`. The tests match against comment-stripped source, so the scripts can explain their own security properties without a grep-based assertion tripping over the explanation.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_publish_ops.py`:

```python
"""Pins the VM-side publisher: dataset-only pushes, token never exposed.

Assertions run against comment-stripped source: the comments exist to explain
the security properties, and a grep-based test must not be satisfied or defeated
by prose.
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
OPS = REPO / "ops"
PUBLISH_SH = OPS / "publish.sh"
ASKPASS_SH = OPS / "git-askpass.sh"
SERVICE = OPS / "ghostbus-publisher.service"
TIMER = OPS / "ghostbus-publisher.timer"
CLASSIFIER = OPS / "ghostbus-classifier.service"

TOKEN_VAR = "GHOSTBUS_PUBLISH_TOKEN"


def code(text: str) -> str:
    """Executable lines only."""
    return "\n".join(line for line in text.splitlines()
                     if line.strip() and not line.strip().startswith("#"))


@pytest.fixture(scope="module")
def publish():
    return code(PUBLISH_SH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def askpass():
    return code(ASKPASS_SH.read_text(encoding="utf-8"))


def test_all_publisher_files_exist():
    for path in (PUBLISH_SH, ASKPASS_SH, SERVICE, TIMER):
        assert path.is_file(), f"missing {path}"


def test_publish_script_is_strict_and_never_traces(publish):
    assert "set -euo pipefail" in publish
    # `set -x` would print the whole environment-derived command stream.
    assert "set -x" not in publish
    assert "set +x" in publish


def test_publish_script_never_names_the_token(publish):
    """Only the askpass helper reads the token."""
    assert TOKEN_VAR not in publish


def test_publish_script_disables_git_tracing(publish):
    # A GIT_TRACE left in /etc/ghostbus.env would put the credential exchange
    # in the journal.
    assert "unset GIT_TRACE" in publish


def test_askpass_prints_the_token_and_nothing_else(askpass):
    assert TOKEN_VAR in askpass
    assert 'printf %s "${GHOSTBUS_PUBLISH_TOKEN}"' in askpass
    assert "echo" not in askpass
    assert "set +x" in askpass


def test_dataset_is_pushed_to_its_own_repository(publish):
    # Split trust: a Contents:write token on the CODE repo could rewrite
    # publish/site.py, which CI checks out and executes.
    assert "github.com/aleks-drozy/ghost-bus-data.git" in publish
    assert "ghost-bus.git" not in publish


def test_push_url_carries_no_credential(publish):
    assert "https://x-access-token@github.com/aleks-drozy/ghost-bus-data.git" in publish
    assert "x-access-token:" not in publish, "no token may be embedded in the URL"


def test_uses_the_dataset_cli_flags_that_exist(publish):
    assert "--data-dir" in publish
    assert "--out " not in publish, "publish.dataset takes --data-dir, not --out"
    assert "--commit" not in publish, "the dataset CLI never touches git"


def test_everything_staged_is_staged_by_explicit_path(publish):
    assert "git add -A" not in publish
    assert "git add ." not in publish
    assert "git commit -a" not in publish
    assert "git add -- ." in publish or "git add --" in publish


def test_aborts_when_the_code_checkout_is_dirty(publish):
    assert "git status --porcelain" in publish
    assert "refusing to publish" in publish


def test_no_op_when_the_dataset_did_not_change(publish):
    assert "git diff --cached --quiet" in publish


def test_gate_failure_stops_before_any_git_command(publish):
    assert publish.index("publish.dataset") < publish.index("git ")


def test_service_matches_the_deployed_layout():
    text = SERVICE.read_text(encoding="utf-8")
    classifier = CLASSIFIER.read_text(encoding="utf-8")
    assert "Type=oneshot" in text
    assert "EnvironmentFile=/etc/ghostbus.env" in text
    assert "EnvironmentFile=/etc/ghostbus.env" in classifier
    assert "WorkingDirectory=/opt/ghost-bus" in text
    assert "WorkingDirectory=/opt/ghost-bus" in classifier
    assert "ExecStart=/opt/ghost-bus/ops/publish.sh" in text


def test_service_runs_after_the_classifier_and_the_network():
    text = SERVICE.read_text(encoding="utf-8")
    assert "ghostbus-classifier.service" in text
    assert "network-online.target" in text


def test_timer_runs_once_a_day_after_the_service_day_closes():
    text = TIMER.read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 03:30 Europe/Dublin" in text
    assert "Persistent=true" in text
    assert "WantedBy=timers.target" in text
    assert "/" not in text.split("OnCalendar=")[1].splitlines()[0]


def test_ops_files_use_unix_line_endings():
    for path in (PUBLISH_SH, ASKPASS_SH, SERVICE, TIMER):
        assert b"\r" not in path.read_bytes(), path.name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_publish_ops.py -q`

Expected: FAIL — `16 failed`. `test_all_publisher_files_exist` fails with `AssertionError: missing ...\ops\publish.sh`; the fixture-backed tests error with `FileNotFoundError: [Errno 2] No such file or directory: '...\\ops\\publish.sh'`; the unit and line-ending tests fail with `FileNotFoundError` on the missing unit files.

- [ ] **Step 3: Write minimal implementation**

Create `C:\Users\Alex\Projects\ghost-bus\ops\publish.sh` (LF line endings, no BOM):

```bash
#!/usr/bin/env bash
# Ghost Bus publisher: build the dataset, then push it to the DATA repository.
#
# Run by ghostbus-publisher.service, which loads /etc/ghostbus.env. The
# credential is never named in this file: git obtains it via ops/git-askpass.sh,
# so it appears neither in argv, nor in `ps` output, nor in the journal.
#
# The dataset lives in its own repository on purpose. A Contents:write token on
# the code repository could rewrite publish/site.py or a template, and CI checks
# that repository out and executes it - arbitrary HTML on the site and arbitrary
# code in CI. Scoped to a repository holding only CSVs and a manifest, the same
# permission cannot reach a line of executable code.
set -euo pipefail
set +x

# A trace variable left in /etc/ghostbus.env would put the credential exchange
# into the journal. Clear them regardless of what the env file carries.
unset GIT_TRACE GIT_TRACE_CURL GIT_TRACE_PACKET GIT_CURL_VERBOSE

REPO_DIR="${GHOSTBUS_REPO_DIR:-/opt/ghost-bus}"
DATA_REPO="${GHOSTBUS_DATA_REPO_DIR:-${REPO_DIR}/data-repo}"
DATA_REMOTE="https://x-access-token@github.com/aleks-drozy/ghost-bus-data.git"
DB_PATH="${GHOSTBUS_DB:-${REPO_DIR}/state/ghostbus.db}"
PY="${REPO_DIR}/.venv/bin/python"

export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS="${REPO_DIR}/ops/git-askpass.sh"

cd "${REPO_DIR}"

# 1. Refuse to run if the code checkout is dirty. The VM is not a development
#    machine; anything modified in place means someone edited the deployed
#    tree, and we will not publish numbers produced by code nobody reviewed.
if [ -n "$(git status --porcelain)" ]; then
  echo "publish: code checkout is dirty - refusing to publish" >&2
  git status --short >&2
  exit 1
fi

# 2. Build the dataset into the data repository's working tree. This enforces
#    the publish gate, the 14-day baseline gate, and complete-service-days-only.
#    A gate failure exits nonzero here and `set -e` stops the run before git is
#    touched at all: nothing is committed, nothing is pushed, and the
#    previously published data stays up.
"${PY}" -m publish.dataset --db "${DB_PATH}" --data-dir "${DATA_REPO}/data"

cd "${DATA_REPO}"

# 3. Stage the dataset by explicit path. Nothing else belongs in this
#    repository, and nothing else is ever swept in.
git add -- .

if git diff --cached --quiet; then
  echo "publish: dataset unchanged, nothing to push"
  exit 0
fi

git -c user.name='ghost-bus publisher' \
    -c user.email='publisher@ghost-bus.invalid' \
    commit -m "data: publish $(date -u +%Y-%m-%dT%H:%M:%SZ)"

git push "${DATA_REMOTE}" HEAD:main

echo "publish: pushed $(git rev-parse --short HEAD)"
```

Create `C:\Users\Alex\Projects\ghost-bus\ops\git-askpass.sh` (LF line endings, no BOM):

```bash
#!/usr/bin/env bash
# git calls this for the password prompt. It writes the credential to stdout
# for git and nowhere else: no logging, no tracing, no trailing newline.
set -euo pipefail
set +x
printf %s "${GHOSTBUS_PUBLISH_TOKEN}"
```

Create `C:\Users\Alex\Projects\ghost-bus\ops\ghostbus-publisher.service` (LF line endings, no BOM):

```
[Unit]
Description=Ghost Bus dataset publish (build CSVs, push to the data repo)
# Ordering only, not a dependency: if both are queued in the same boot the
# classifier finishes first, so the day being published is fully classified.
After=ghostbus-classifier.service
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/ghost-bus
EnvironmentFile=/etc/ghostbus.env
ExecStart=/opt/ghost-bus/ops/publish.sh
```

Create `C:\Users\Alex\Projects\ghost-bus\ops\ghostbus-publisher.timer` (LF line endings, no BOM):

```
[Unit]
Description=Publish the Ghost Bus dataset daily

[Timer]
# 03:30 local: late enough that the previous service day is definitively
# closed and the classifier has had several passes at it, so the
# complete-service-days-only rule has a whole day to publish.
OnCalendar=*-*-* 03:30 Europe/Dublin
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_publish_ops.py -q; python -m pytest -q`

Expected: PASS — `16 passed` for the file, full suite green with 0 failed, 0 errors.

- [ ] **Step 5: Commit**

The executable bits do not survive a Windows checkout, so set them in the index as part of this commit (the RUNBOOK also runs `chmod +x` on the VM, belt and braces):

```
cd C:\Users\Alex\Projects\ghost-bus
git add ops/publish.sh ops/git-askpass.sh ops/ghostbus-publisher.service ops/ghostbus-publisher.timer tests/test_publish_ops.py
git update-index --chmod=+x ops/publish.sh ops/git-askpass.sh
git commit -m "ops: daily VM publisher that pushes the dataset to its own repo

publish.sh builds the dataset (gate first, so a failure pushes nothing) into a
checkout of ghost-bus-data and pushes only that. The dataset lives in its own
repository so the VM's Contents:write token cannot reach publish/site.py or a
template - which CI checks out and executes. The credential comes from
/etc/ghostbus.env via GIT_ASKPASS, never entering argv or the journal, and
git tracing is unset regardless of the env file. Daily oneshot at 03:30
Europe/Dublin, ordered after the classifier.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 19: Publish workflow — test suite, site build, GitHub Pages deploy

**Precondition:** `publish/site.py` must expose `main`. Verify with `python -m publish.site --help` — expect `--data`, `--out`, `--site`. If it fails, Task 16 is not done — stop.

**Files:**
- Create: `C:\Users\Alex\Projects\ghost-bus\.github\workflows\publish.yml`
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_publish_ci.py`

**Interfaces:**
- Consumes: `python -m publish.site --data <data_dir> --out <out_dir>` (Task 16).
- Consumes: the dataset repository `aleks-drozy/ghost-bus-data` (Task 18), checked out into `./data`.
- Produces: a Pages deployment of `_site/` at `https://aleks-drozy.github.io/ghost-bus/`, triggered by pushes to `main` touching the site sources and by `repository_dispatch` from the data repo. Tasks 20 and 21 reference that URL.

No PyYAML is available (`requirements-dev.txt` is `-r requirements.txt` plus `pytest>=8.0`), and the hard constraint is stdlib only — so the workflow is pinned by exact-substring assertions on its text: the permissions block, the test-before-build ordering, and the action versions.

**Before writing the file, confirm the action tags exist** by opening each action's releases page on github.com. The test can only assert that a string is present, so a nonexistent tag would pass the suite and fail at deploy time with `Unable to resolve action`. If a tag has moved on, use the current major and update the assertion in the same edit.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_publish_ci.py`:

```python
"""Pins the publish workflow: gates, permissions, ordering, action versions.

Text assertions, not YAML parsing - the project is stdlib-only and PyYAML is
not a dev dependency. What matters is that specific lines exist and that the
test step precedes the build step.
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "publish.yml"


@pytest.fixture(scope="module")
def text():
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_file_exists():
    assert WORKFLOW.is_file(), f"missing workflow: {WORKFLOW}"


def test_triggers_on_site_source_changes_and_on_new_data(text):
    assert "branches: [main]" in text
    # A change to the builder or a template must redeploy: otherwise the live
    # site keeps serving stale HTML until the data happens to move.
    for path in ("'publish/**'", "'site/**'", "'.github/workflows/publish.yml'"):
        assert path in text, path
    # The dataset lives in another repo, so it signals by dispatch.
    assert "repository_dispatch:" in text
    assert "types: [dataset-published]" in text
    assert "workflow_dispatch:" in text


def test_has_pages_permissions_and_no_write_all(text):
    assert "contents: read" in text
    assert "pages: write" in text
    assert "id-token: write" in text
    assert "permissions: write-all" not in text
    assert "contents: write" not in text


def test_has_a_serialising_concurrency_group(text):
    assert "concurrency:" in text
    assert "group: pages" in text
    assert "cancel-in-progress: false" in text


def test_checks_out_the_dataset_repository_into_the_data_directory(text):
    assert "repository: aleks-drozy/ghost-bus-data" in text
    assert "path: data" in text


def test_full_suite_runs_before_the_site_is_built(text):
    suite = text.index("run: python -m pytest")
    build = text.index("run: python -m publish.site")
    assert suite < build, "the test suite must run before the site is built"


def test_builds_the_site_from_the_published_csvs_only(text):
    assert "run: python -m publish.site --data data/data --out _site" in text
    # D3: the site is never built from the database.
    assert "ghostbus.db" not in text
    assert "publish.dataset" not in text


def test_uses_pinned_pages_actions(text):
    for action in (
        "actions/checkout@v5",
        "actions/setup-python@v6",
        "actions/configure-pages@v5",
        "actions/upload-pages-artifact@v3",
        "actions/deploy-pages@v4",
    ):
        assert action in text, f"expected {action}"


def test_setup_python_matches_the_tests_workflow(text):
    assert "python-version: '3.12'" in text
    assert "cache: pip" in text
    assert "pip install -r requirements-dev.txt" in text


def test_deploy_job_depends_on_build_and_targets_the_pages_environment(text):
    assert "needs: build" in text
    assert "name: github-pages" in text
    assert "url: ${{ steps.deployment.outputs.page_url }}" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_publish_ci.py -q`

Expected: FAIL — `10 failed`. `test_workflow_file_exists` fails with `AssertionError: missing workflow: ...\.github\workflows\publish.yml`, and every fixture-backed test errors with `FileNotFoundError: [Errno 2] No such file or directory: '...\\.github\\workflows\\publish.yml'`.

- [ ] **Step 3: Write minimal implementation**

Create `C:\Users\Alex\Projects\ghost-bus\.github\workflows\publish.yml`:

```yaml
name: publish
on:
  push:
    branches: [main]
    # Any change that can alter the emitted HTML must redeploy, or the live
    # site keeps serving stale pages until the data happens to move.
    paths: ['publish/**', 'site/**', 'aggregate/**', '.github/workflows/publish.yml']
  # The dataset lives in aleks-drozy/ghost-bus-data, so a new publish arrives
  # as a dispatch rather than as a push to this repository.
  repository_dispatch:
    types: [dataset-published]
  workflow_dispatch:

# Least privilege: the workflow reads the repos and writes only a Pages
# deployment. It never writes repo contents - the VM owns the dataset.
permissions:
  contents: read
  pages: write
  id-token: write

# One Pages deployment at a time; never cancel one mid-flight, because a
# cancelled deploy can leave the site on a half-uploaded artifact.
concurrency:
  group: pages
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      # The published dataset, checked out alongside the code. It contains only
      # CSVs and a manifest, so nothing here is executable.
      - uses: actions/checkout@v5
        with:
          repository: aleks-drozy/ghost-bus-data
          path: data
      - uses: actions/setup-python@v6
        with: {python-version: '3.12', cache: pip}
      - run: pip install -r requirements-dev.txt
      # Gate: the full suite must pass before anything is rendered.
      - run: python -m pytest
      # D3: the site is built from the published CSVs, never from the database.
      - run: python -m publish.site --data data/data --out _site
      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with: {path: _site}

  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - id: deployment
        uses: actions/deploy-pages@v4
```

Two one-time repository settings, done by the owner in the GitHub UI (neither can be scripted from here); both are recorded in the RUNBOOK in Task 20:

1. **Settings → Pages → Build and deployment → Source: GitHub Actions.** Until this is set, `actions/configure-pages@v5` fails with `Get Pages site failed`.
2. A workflow in `aleks-drozy/ghost-bus-data` that fires `repository_dispatch` with type `dataset-published` at this repo on push, using a token with `Contents: read` and `Actions: write` on `ghost-bus` **only**. (Manual `workflow_dispatch` works in the meantime.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_publish_ci.py -q; python -m pytest -q`

Expected: PASS — `10 passed` for the file, full suite green with 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```
cd C:\Users\Alex\Projects\ghost-bus
git add .github/workflows/publish.yml tests/test_publish_ci.py
git commit -m "ci: build and deploy the scoreboard to GitHub Pages

Checks out the dataset repository alongside the code, runs the full suite
first, then builds _site from the published CSVs only (D3), then deploys via
the Pages actions with pages:write + id-token:write and a serialising
concurrency group. Triggers on site-source pushes as well as on new data, so
a builder or template change cannot leave stale HTML live. Workflow contents
pinned by tests.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 20: RUNBOOK section 8 — publishing, verification, gate failures, token rotation

**Files:**
- Modify: `C:\Users\Alex\Projects\ghost-bus\ops\RUNBOOK.md:540` (append a new `## 8.` section after the final line; the file currently ends with section 7.2)
- Test: `C:\Users\Alex\Projects\ghost-bus\tests\test_publish_docs.py` (create)

**Interfaces:**
- Consumes: `ops/publish.sh`, `ops/git-askpass.sh`, `ops/ghostbus-publisher.service`, `ops/ghostbus-publisher.timer` (Task 18); `.github/workflows/publish.yml` (Task 19); `data/manifest.json` field `scoreboard_ready` (Task 8).
- Produces: the operator procedure Task 21's README links to.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\Alex\Projects\ghost-bus\tests\test_publish_docs.py`:

```python
"""Pins the operator-facing publishing documentation.

Docs rot silently. These assertions cover the parts an operator would be
harmed by missing: the token's scope and storage rules, the "do nothing"
answer to a gate failure, and the rotation procedure.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RUNBOOK = REPO / "ops" / "RUNBOOK.md"


@pytest.fixture(scope="module")
def runbook():
    return RUNBOOK.read_text(encoding="utf-8")


def section(text, heading):
    """Return the text of a '## ' section, up to the next '## ' heading."""
    start = text.index(heading)
    rest = text[start + len(heading):]
    match = re.search(r"^## ", rest, flags=re.MULTILINE)
    return rest[: match.start()] if match else rest


def test_runbook_has_a_publishing_section(runbook):
    assert "## 8. Publishing" in runbook


def test_publishing_section_covers_install_and_the_daily_timer(runbook):
    body = section(runbook, "## 8. Publishing")
    assert "ghostbus-publisher.timer" in body
    assert "systemctl enable --now ghostbus-publisher.timer" in body
    assert "chmod +x /opt/ghost-bus/ops/publish.sh" in body


def test_publishing_section_states_the_token_scope_and_storage(runbook):
    body = section(runbook, "## 8. Publishing")
    assert "GHOSTBUS_PUBLISH_TOKEN" in body
    assert "/etc/ghostbus.env" in body
    assert "chmod 600 /etc/ghostbus.env" in body
    assert "Contents: Read and write" in body
    assert "ghost-bus-data" in body
    assert "never be echoed" in body


def test_publishing_section_explains_why_the_dataset_has_its_own_repo(runbook):
    body = section(runbook, "## 8. Publishing")
    assert "cannot reach" in body
    assert "publish/site.py" in body


def test_publishing_section_documents_the_pages_source_setting(runbook):
    body = section(runbook, "## 8. Publishing")
    assert "Settings -> Pages" in body
    assert "Source: GitHub Actions" in body


def test_publishing_section_uses_the_real_cli_flags(runbook):
    body = section(runbook, "## 8. Publishing")
    assert "--data-dir" in body
    assert "publish.dataset --db state/ghostbus.db --out " not in body


def test_gate_failure_procedure_says_publish_nothing(runbook):
    body = section(runbook, "## 8. Publishing")
    assert "### 8.4" in body
    assert "Nothing was published" in body
    assert "previously published data stays up" in body
    assert "Do not force a publish" in body


def test_rotation_procedure_is_documented(runbook):
    body = section(runbook, "## 8. Publishing")
    assert "### 8.5 Rotating the publish token" in body
    assert "systemctl restart" in body or "daemon-reload" in body


def test_pre_baseline_mode_is_diagnosable(runbook):
    body = section(runbook, "## 8. Publishing")
    assert "scoreboard_ready" in body
    assert "complete_days" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_publish_docs.py -q`

Expected: FAIL — `9 failed`. `test_runbook_has_a_publishing_section` fails with `AssertionError: assert '## 8. Publishing' in '# Ghost Bus Tracker — Ops Runbook...'`, and the remaining eight fail with `ValueError: substring not found` raised from `section()`.

- [ ] **Step 3: Write minimal implementation**

Append the following to the end of `C:\Users\Alex\Projects\ghost-bus\ops\RUNBOOK.md` (after the current final line 540):

```markdown

---

## 8. Publishing (dataset -> GitHub Pages)

Publishing is split in two on purpose. The **VM** produces the dataset and
pushes it to a *separate* repository, `aleks-drozy/ghost-bus-data`, which holds
nothing but CSVs and a manifest. **CI** checks that repository out beside the
code, renders the site from those CSVs, and deploys it.

The separation is what makes the trust boundary real. A token with write
access to the *code* repository could rewrite `publish/site.py` or a template —
and CI checks the code repository out and executes it, so that is arbitrary
HTML on the public site and arbitrary code in the CI runner. Scoped to a
repository containing only data, the same permission **cannot reach**
`publish/site.py`, any template, or any test. The worst a compromised VM can
do is corrupt numbers, which are public, diffable, and recomputable.

### 8.1 One-time GitHub setup (owner only)

Three steps that cannot be scripted from the VM:

1. **Create the data repository.** A new public repo `aleks-drozy/ghost-bus-data`
   with a `main` branch and nothing in it but a README. The publisher writes
   `data/manifest.json`, `data/daily/`, `data/uptime/` into it. Nothing
   executable ever belongs there.
2. **Enable Pages from Actions** on the *code* repo:
   **Settings -> Pages -> Build and deployment -> Source: GitHub Actions**.
   Until this is set, every publish run fails at `actions/configure-pages`
   with `Get Pages site failed`.
3. **Mint the publish token.** A **fine-grained personal access token**, not a
   classic one:
   **Settings -> Developer settings -> Personal access tokens ->
   Fine-grained tokens -> Generate new token**
   - Resource owner: `aleks-drozy`
   - Repository access: **Only select repositories** -> `ghost-bus-data`.
     **Not `ghost-bus`.** This is the whole point of the split; a token that
     can also write the code repo gives back everything the separation bought.
   - Repository permissions: **Contents: Read and write**. Nothing else —
     leave Actions, Workflows, Pages, Secrets and every other permission at
     **No access**.
   - Expiration: 90 days. Put the rotation date in the calendar; §8.5 is the
     procedure.

   Copy the `github_pat_...` value once — GitHub will not show it again.

   Optionally, add a workflow in `ghost-bus-data` that fires a
   `repository_dispatch` of type `dataset-published` at `ghost-bus` on push,
   so a new dataset redeploys the site automatically. Until that exists, run
   the **publish** workflow from the Actions tab by hand after a data push.

### 8.2 Install the publisher on the VM

The token goes in the same file as the NTA key, with the same permissions.
That file is the only copy of either secret on the box.

```bash
cd /opt/ghost-bus
git pull

# Clone the data repository once. The publisher writes into it and pushes it.
git clone https://github.com/aleks-drozy/ghost-bus-data.git /opt/ghost-bus/data-repo

# Append the token to the existing env file. Note the leading space: with
# HISTCONTROL=ignorespace (bash default on Ubuntu) the line stays out of
# ~/.bash_history. Paste the real token in place of github_pat_xxx.
 sudo sh -c 'printf "GHOSTBUS_PUBLISH_TOKEN=%s\n" "github_pat_xxx" >> /etc/ghostbus.env'

sudo chmod 600 /etc/ghostbus.env
sudo chown root:root /etc/ghostbus.env

# Confirm it landed without printing the value:
sudo grep -c '^GHOSTBUS_PUBLISH_TOKEN=' /etc/ghostbus.env    # expect: 1
```

**The token must never be echoed into logs.** `ops/publish.sh` never names the
variable; git receives it through `ops/git-askpass.sh`, which writes it to
git's stdin-substitute and nowhere else, so it appears neither in `ps` output
nor in the journal. The script also unsets `GIT_TRACE` and friends, so a
debugging variable left in the env file cannot leak the exchange. Do not add
`set -x` to either script, do not echo the variable while debugging, and do
not paste the value into an issue, a commit, or a chat window. If you ever see
it in `journalctl -u ghostbus-publisher.service`, treat the token as burned and
go straight to §8.5.

Install the units:

```bash
chmod +x /opt/ghost-bus/ops/publish.sh /opt/ghost-bus/ops/git-askpass.sh
sudo cp /opt/ghost-bus/ops/ghostbus-publisher.service /etc/systemd/system/
sudo cp /opt/ghost-bus/ops/ghostbus-publisher.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ghostbus-publisher.timer
systemctl list-timers ghostbus-publisher.timer --no-pager
```

The timer fires daily at **03:30 Europe/Dublin** — late enough that the
previous service day is closed and the classifier has finished with it, so the
"complete service days only" rule has a whole day to publish. `Persistent=true`
means a reboot across 03:30 runs the publish on the next boot rather than
skipping the day.

### 8.3 Verifying a publish

Run it once by hand rather than waiting for the timer:

```bash
sudo systemctl start ghostbus-publisher.service
journalctl -u ghostbus-publisher.service -n 40 --no-pager
```

A healthy run ends with one of two lines: `publish: pushed <sha>` or
`publish: dataset unchanged, nothing to push`. Then check, in order:

```bash
# 1. The dataset on the VM.
cat /opt/ghost-bus/data-repo/data/manifest.json
ls -l /opt/ghost-bus/data-repo/data/uptime | tail -5
ls -l /opt/ghost-bus/data-repo/data/daily  | tail -5     # empty before the baseline

# 2. The commit that was pushed.
cd /opt/ghost-bus/data-repo && git show --stat HEAD
```

3. On github.com, the **publish** workflow run should be green. It runs the
   full test suite *before* it builds, so a red run means either a genuine test
   failure or that the site builder raised on real data — in both cases the
   previous site stays live and nothing is deployed.
4. Load `https://aleks-drozy.github.io/ghost-bus/` and confirm the uptime
   strip's latest date matches `coverage.last_day` in the manifest.

### 8.4 When the publish gate fails

**Nothing was published, and that is the system working.** `publish.sh` runs
`publish/dataset.py` first; a failed gate exits nonzero, `set -e` stops the
script before any git command, and the run never reaches a commit. The
**previously published data stays up** — stale but verified — rather than being
replaced by numbers that failed their own checks.

You will see it as a failed unit:

```bash
systemctl --failed
journalctl -u ghostbus-publisher.service -n 60 --no-pager
```

Investigate before doing anything else:

```bash
cd /opt/ghost-bus
.venv/bin/python run_checks.py                       # which check failed, and why
.venv/bin/python -m publish.dataset --db state/ghostbus.db --data-dir /tmp/ghostbus-dryrun
```

`outcomes_valid` runs first as a gate, so if it fails, fix that before reading
anything into conservation or bounded-rates output. A conservation failure
means trips are being lost or double-counted between the timetable and the
outcomes table; a bounded-rates failure means a rate fell outside `[0, 1]`, or
a point estimate sat outside its own interval, which is a computation bug and
not a data quirk.

**Do not force a publish past a failed gate.** There is no flag for it and none
should be added: the whole value of the project is that a published number is
one the data supports. Fix the cause, re-run `.venv/bin/python run_checks.py`
until it is clean, then `sudo systemctl start ghostbus-publisher.service`.

The one *non*-failure that looks like one: a run that exits 0 saying
`dataset unchanged, nothing to push`. That is normal before the 14-day
baseline, when only the manifest and uptime CSVs exist and neither has moved.
It is **not** normal on a day when uptime should have changed — if you see it
repeatedly, check that `data/` is not being ignored by git in the data
repository.

### 8.5 Rotating the publish token

Rotate on expiry, on any suspicion of exposure, and whenever someone who has
had shell on the box no longer should.

1. Mint the replacement first, per §8.1 step 3 (same scope: `ghost-bus-data`
   only, **Contents: Read and write**, nothing else).
2. Replace the line in place, without printing it:

   ```bash
   sudo sed -i '/^GHOSTBUS_PUBLISH_TOKEN=/d' /etc/ghostbus.env
    sudo sh -c 'printf "GHOSTBUS_PUBLISH_TOKEN=%s\n" "github_pat_new" >> /etc/ghostbus.env'
   sudo chmod 600 /etc/ghostbus.env
   sudo grep -c '^GHOSTBUS_PUBLISH_TOKEN=' /etc/ghostbus.env    # expect: 1
   ```

3. `systemd` reads `EnvironmentFile` at each start of the oneshot unit, so no
   restart of a long-running process is needed — but reload and prove it works
   before you trust it:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl start ghostbus-publisher.service
   journalctl -u ghostbus-publisher.service -n 20 --no-pager
   ```

4. **Revoke the old token on github.com** (Settings -> Developer settings ->
   Fine-grained tokens -> the old token -> Delete). Rotation is not finished
   until the old one is dead — a token that still works is still a key.
5. If the rotation was triggered by suspected exposure, check the data repo's
   history for anything the old token pushed that is not a CSV or the manifest:
   `cd /opt/ghost-bus/data-repo && git log --name-only --since="30 days ago"`.
   The site builder refuses to publish an unexpected file, so such a push would
   have turned CI red rather than reaching the site — but it should still be
   found and removed.

### 8.6 Is the site in pre-baseline mode?

Pre-baseline mode is not a setting — it is a computed consequence of how many
complete service days exist. Read it from the manifest, which is the same
input the site builder uses:

```bash
python3 -c "import json;m=json.load(open('/opt/ghost-bus/data-repo/data/manifest.json'));print(m['scoreboard_ready'], m['coverage']['complete_days'], m['baseline_required_days'])"
```

- `False 9 14` -> pre-baseline. `data/daily/` is empty by design, the site
  renders methodology, the uptime strip, and "collecting baseline — day 9 of
  14". No route table, no route pages. **This is correct behaviour, not an
  outage.** Uptime is deliberately exempt from the gate and publishes from day
  one: it is our own downtime, not a claim about any operator.
- `True 14 14` -> the scoreboard is live. `data/daily/` has one CSV per
  complete service day and `index.html` carries the ranked table.

The gate is a state, not an event: if coverage ever falls back below 14 days,
the publisher **withdraws** the route CSVs and the site returns to pre-baseline
mode. That is intended. A site saying "we publish nothing about any route" must
not be linking route data.

From the outside, without shell access: fetch
`https://aleks-drozy.github.io/ghost-bus/data/manifest.json`, or just look at
the front page — pre-baseline mode says so in plain English on the page.

### 8.7 Publishing from a fresh checkout after a VM rebuild

The VM's `data-repo` is a working copy of what is already public, so a rebuilt
box does not need any of it restored: `git clone` brings the published dataset
back, and the next publish adds to it. The only things that must be recreated
by hand are the clone (§8.2) and `/etc/ghostbus.env` (§2.1 for the NTA key,
§8.2 for the publish token).
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_publish_docs.py -q; python -m pytest -q`

Expected: PASS — `9 passed` for the file, full suite green with 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```
cd C:\Users\Alex\Projects\ghost-bus
git add ops/RUNBOOK.md tests/test_publish_docs.py
git commit -m "docs: runbook section 8 - publishing, gate failures, token rotation

Covers installing the publisher timer, verifying a publish end to end, the
fine-grained token (ghost-bus-data only, Contents: Read and write, stored in
/etc/ghostbus.env mode 600, never echoed) and why the dataset has its own
repository, rotation, and how to tell whether the site is in pre-baseline
mode. A failed gate publishes nothing and is not to be forced.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 21: README — public-facing scoreboard and open-data section

**Files:**
- Modify: `C:\Users\Alex\Projects\ghost-bus\README.md` (update the repo-tree block at lines 155-160; insert a new `## The scoreboard & open data` section immediately before the `## Docs` heading at line 162; fold the attribution paragraph into the existing `## Data & attribution` section at line 172)
- Modify: `C:\Users\Alex\Projects\ghost-bus\tests\test_publish_docs.py` (append README tests)

**Interfaces:**
- Consumes: the site URL and dataset layout established in Tasks 18–20.
- Produces: nothing other tasks depend on.

The honesty constraint is testable: the section must contain no percentage, because any number in a README goes stale the moment the data moves, and a stale reliability figure is exactly the kind of overclaim the spec forbids.

- [ ] **Step 1: Write the failing test**

Append to `C:\Users\Alex\Projects\ghost-bus\tests\test_publish_docs.py`:

```python


README = REPO / "README.md"


@pytest.fixture(scope="module")
def readme():
    return README.read_text(encoding="utf-8")


def test_readme_has_a_scoreboard_section(readme):
    assert "## The scoreboard & open data" in readme


def test_readme_links_the_site_and_the_dataset(readme):
    body = section(readme, "## The scoreboard & open data")
    assert "https://aleks-drozy.github.io/ghost-bus/" in body
    assert "ghost-bus-data" in body
    assert "daily/" in body
    assert "uptime/" in body
    assert "manifest.json" in body


def test_readme_states_the_baseline_gate(readme):
    body = section(readme, "## The scoreboard & open data")
    assert "14 complete service days" in body


def test_readme_states_the_two_rates_are_never_summed(readme):
    body = section(readme, "## The scoreboard & open data")
    assert "never summed" in body


def test_readme_gate_copy_counts_trips_judged(readme):
    body = section(readme, "## The scoreboard & open data")
    assert "30 trips we could judge" in body
    assert "30 scheduled trips" not in body


def test_readme_publishes_no_reliability_numbers(readme):
    """No percentages: any figure here is stale the next time data lands."""
    body = section(readme, "## The scoreboard & open data")
    assert not re.search(r"\d+(\.\d+)?\s*%", body), "no reliability figures in the README"


def test_readme_tree_lists_the_new_packages(readme):
    assert "publish/" in readme
    assert "site/" in readme


def test_attribution_appears_once(readme):
    assert readme.count("National Transport Authority") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_publish_docs.py -q`

Expected: FAIL — `10 passed, 7 failed`. `test_readme_has_a_scoreboard_section` fails with `AssertionError: assert '## The scoreboard & open data' in '# Ghost Bus Tracker...'`; the five section-backed tests fail with `ValueError: substring not found` from `section()`; `test_readme_tree_lists_the_new_packages` fails with `AssertionError: assert 'publish/' in ...`. `test_attribution_appears_once` passes already.

- [ ] **Step 3: Write minimal implementation**

First, in the repo-tree block at `README.md:155-160`, add two lines beside the existing `classify/ aggregate/ ops/ tests/` entries, matching the surrounding format:

```
publish/            dataset publisher (VM) and site builder (CI)
site/               HTML templates and the one stylesheet
```

Second, insert immediately before the `## Docs` heading (current line 162):

```markdown
## The scoreboard & open data

**Site:** https://aleks-drozy.github.io/ghost-bus/
**Data:** https://github.com/aleks-drozy/ghost-bus-data

Every number on the scoreboard is computed from files you can download and
recompute yourself. The site is built in CI *from the published CSVs*, never
from the database, so a figure on the page that disagreed with the data would
have to come from nowhere — there is only one source. The dataset lives in its
own repository, written by a credential that can reach nothing else, so the
machine that produces the numbers cannot produce the page.

| Path in the data repo | What it is |
|---|---|
| `data/daily/` | One CSV per complete service day: per-route counts of every outcome, plus both rates with their 95% confidence intervals |
| `data/uptime/` | One CSV per day of the tracker's own uptime — the downtime that excludes trips from operator stats |
| `data/manifest.json` | Coverage dates, timetable hash and load date, gate results, schema version, and any route ids we could not name |

What the site will and will not say:

- **Two rates, never summed.** VANISHED (a bus was tracked, then disappeared
  mid-route) and UNTRACKED (we never saw it at all) are published as separate
  columns. Summing them would assert that every untracked trip failed to run,
  and we cannot know that — a dead telematics unit looks identical to a bus
  that never left the depot.
- **Ranked by the bottom of the confidence interval, not the headline rate.**
  A route is placed above another only where the sample supports the ordering.
  Ranking uses the vanished rate only; the untracked rate never affects
  position, because ranking on it would rank operators by how well their
  tracking hardware works.
- **Routes with fewer than 30 trips we could judge in the window are listed
  but never ranked, and no headline rate is claimed for them.** The count that
  matters is trips we were actually watching — scheduled minus excluded — so a
  route we mostly missed cannot be shamed on the handful we saw.
- **Nothing route-level is published until 14 complete service days exist.**
  Until then the site shows the methodology, the tracker's own uptime, and how
  far into the baseline we are. Uptime is exempt from that gate and publishes
  from day one — it is our own failure rate, not an accusation about anyone
  else. If coverage ever falls back below the threshold, the route data is
  withdrawn again.
- **Only complete service days count.** A day in progress understates trips
  and distorts every rate computed from it, so today is never published.
- **Gaps are shown as gaps.** A day with no data is never interpolated, and a
  rate with no trips behind it renders as "—", never as zero.

The site is plain HTML and one stylesheet: no JavaScript, no analytics, no
cookies, no fonts or assets from anywhere else. It makes no third-party
requests at all.

```

Third, do **not** add a new attribution paragraph — `README.md:172` already has
`## Data & attribution`. Append one sentence to that existing section instead:

```markdown
The published dataset in `aleks-drozy/ghost-bus-data` carries the same
attribution: the classifications, rates and intervals in those files are ours,
not the NTA's. If you use them, link to the methodology so whoever reads your
work can see what the numbers do and do not claim.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\Alex\Projects\ghost-bus; python -m pytest tests/test_publish_docs.py -q; python -m pytest -q`

Expected: PASS — `17 passed` for the file, full suite green with 0 failed, 0 errors.

- [ ] **Step 5: Commit**

```
cd C:\Users\Alex\Projects\ghost-bus
git add README.md tests/test_publish_docs.py
git commit -m "docs: README section for the scoreboard and the open dataset

Points at the site and the data repository, states the split rates, the Wilson
lower bound ranking, the 30-judged-trip and 14-day gates, and the
no-JS/no-analytics property. Carries no reliability figures - a test pins that,
so the section cannot go stale into an overclaim. Adds publish/ and site/ to
the repo tree and folds the new attribution line into the existing section
rather than repeating it.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 22: Vault docs — status, decisions, known issues

**Files:**
- Modify: `C:\Users\Alex\ObsidianVault\claude-memory\19-ghost-bus\_INDEX.md` (frontmatter `updated:`, the `Last updated:` line, the "Public repo" line, and a new first bullet under `## Status`)
- Modify: `C:\Users\Alex\ObsidianVault\claude-memory\19-ghost-bus\DECISIONS.md` (frontmatter `updated:`, `Last updated:` line, five new entries inserted at the top of the entry list — decisions are most-recent-first)
- Modify: `C:\Users\Alex\ObsidianVault\claude-memory\19-ghost-bus\KNOWN_ISSUES.md` (frontmatter `updated:`, new top section)
- Test: a PowerShell verification block — the vault lives outside the repo and is not on `pytest`'s `testpaths`, and a public repo must not carry a test that reads a machine-local absolute path.

**Interfaces:**
- Consumes: everything shipped in Tasks 1–21.
- Produces: nothing code depends on. This discharges the project's standing order to refresh vault docs in the same turn as the feature.

**Resolve the two bracketed values before writing.** Run both commands and substitute the real values; the Step 4 check fails if either placeholder survives.

```
git -C C:\Users\Alex\Projects\ghost-bus rev-parse --short HEAD
cd C:\Users\Alex\Projects\ghost-bus; python -m pytest -q
```

`<COMMIT>` is the short SHA; `<COUNT>` is the number from the `N passed` line.

- [ ] **Step 1: Write the failing test**

The check is this PowerShell block. It prints `PUBLISHER-DOCS-OK` only when all three files carry the new content and no placeholder survives:

```powershell
$v = "C:\Users\Alex\ObsidianVault\claude-memory\19-ghost-bus"
$ok = $true
if (-not (Select-String -Path "$v\_INDEX.md"       -SimpleMatch "P4 SHIPPED" -Quiet)) { $ok = $false; Write-Output "MISSING: _INDEX status line" }
if (-not (Select-String -Path "$v\DECISIONS.md"    -SimpleMatch "Two rates, never summed" -Quiet)) { $ok = $false; Write-Output "MISSING: split-rates decision" }
if (-not (Select-String -Path "$v\DECISIONS.md"    -SimpleMatch "Wilson lower bound" -Quiet)) { $ok = $false; Write-Output "MISSING: Wilson decision" }
if (-not (Select-String -Path "$v\DECISIONS.md"    -SimpleMatch "split trust" -Quiet)) { $ok = $false; Write-Output "MISSING: split-trust decision" }
if (-not (Select-String -Path "$v\DECISIONS.md"    -SimpleMatch "gate counts trips judged" -Quiet)) { $ok = $false; Write-Output "MISSING: judged-trips gate decision" }
if (-not (Select-String -Path "$v\DECISIONS.md"    -SimpleMatch "slug map lives in the published dataset" -Quiet)) { $ok = $false; Write-Output "MISSING: slug-map decision" }
if (-not (Select-String -Path "$v\KNOWN_ISSUES.md" -SimpleMatch "baseline gate" -Quiet)) { $ok = $false; Write-Output "MISSING: known-issues baseline entry" }
if (Select-String -Path "$v\_INDEX.md" -SimpleMatch "<COMMIT>" -Quiet) { $ok = $false; Write-Output "MISSING: <COMMIT> placeholder not filled in" }
if (Select-String -Path "$v\_INDEX.md" -SimpleMatch "<COUNT>" -Quiet) { $ok = $false; Write-Output "MISSING: <COUNT> placeholder not filled in" }
if ($ok) { Write-Output "PUBLISHER-DOCS-OK" }
```

- [ ] **Step 2: Run test to verify it fails**

Run the PowerShell block above.

Expected: FAIL — prints seven `MISSING:` lines (`_INDEX status line`, `split-rates decision`, `Wilson decision`, `split-trust decision`, `judged-trips gate decision`, `slug-map decision`, `known-issues baseline entry`) and does **not** print `PUBLISHER-DOCS-OK`. The two placeholder checks do not fire yet, because the lines containing them do not exist.

- [ ] **Step 3: Write minimal implementation**

**`_INDEX.md`** — set the frontmatter `updated: 2026-07-20`, set `Last updated: 2026-07-20`, replace the "Public repo" line with:

```markdown
**Public repo:** github.com/aleks-drozy/ghost-bus (code) ·
github.com/aleks-drozy/ghost-bus-data (published dataset) · scoreboard live at
https://aleks-drozy.github.io/ghost-bus/ — in pre-baseline mode until 14
complete service days exist, showing methodology + tracker uptime only
```

and insert this as the **first** bullet under `## Status` (with `<COMMIT>` and `<COUNT>` replaced by the real values from the commands above):

```markdown
- 2026-07-20: **P4 SHIPPED — the publisher and the public scoreboard.** Main at
  `<COMMIT>`, <COUNT>/<COUNT> tests. The last phase of the original spec: the
  VM builds a dataset (`data/daily/*.csv`, `data/uptime/*.csv`,
  `data/manifest.json`) behind the publish gate every night at 03:30
  Europe/Dublin and pushes it to a **separate data repository**; CI renders the
  site *from those CSVs* and deploys to GitHub Pages. Four things changed
  methodology rather than plumbing: the combined ghost rate is **gone**,
  replaced by separate vanished and untracked rates that no code path sums;
  every rate carries a **95% Wilson interval** and the leaderboard ranks by the
  **lower bound of the vanished rate**; the ranking gate counts **trips we
  could judge**, not trips scheduled; and the 14-day baseline is a **state**,
  so falling below it withdraws route data rather than leaving it standing.
  `tests/test_rollup.py` was rewritten in the same commit as the rate split,
  since it asserted the old combined value; `.gitignore` had to stop ignoring
  `data/`, without which the whole pipeline would have been a silent no-op.
  **The site is live but deliberately empty of route data** — the baseline
  clock started 2026-07-18, so route numbers appear once 14 complete days
  exist. Details: [[DECISIONS]], [[KNOWN_ISSUES]], `ops/RUNBOOK.md` §8.
```

**`DECISIONS.md`** — set the frontmatter `updated: 2026-07-20` and `Last updated: 2026-07-20`, then insert these five entries immediately after the `---` that follows the `Every non-obvious decision, most recent first.` line, above the existing 2026-07-19 entries:

```markdown
## 2026-07-20 — Two rates, never summed
*(A methodology **definition**, not an amendment. No G-number: nothing was ever
published under the old combined rate, so no public claim is being revised.)*
**Decision:** `_ghost_rate` — `(untracked + vanished) / (scheduled - excluded)` —
is removed. VANISHED and UNTRACKED are computed, published, and displayed as two
separate rates, and **no code path sums them**. A test asserts no published row
exposes a field equal to `vanished + untracked` and that no such combined key
exists.
**Why:** the two are different claims. VANISHED is direct evidence: a vehicle was
observed on the trip and then stopped reporting mid-route. UNTRACKED is the
commuter's ghost *and* what a dead telematics unit looks like — indistinguishable
from our vantage point. The README has said since day one that UNTRACKED is
reported as untracked, never as "did not run"; a summed headline rate would have
contradicted our own methodology page on the front page of the site. The
denominator stays `scheduled - excluded` for both: tracker downtime never counts
against the operator.
**Impact:** `aggregate/rollup.py` (both rollups), `run_checks.py`
(`check_rates_bounded` validates both), the dataset CSV schema, the site.
**`tests/test_rollup.py` was updated in the same commit** — it asserted
`ghost_rate == approx((1+1)/(5-1))` and a `None` rate for an all-excluded route;
both now assert against the split fields.

## 2026-07-20 — Wilson intervals, rank by the lower bound of the vanished rate
**Decision:** every published rate carries a 95% Wilson score interval
(`aggregate/rates.py`, stdlib `math` only, `None` at `trials <= 0`, bounds
clamped to `[0,1]`). The leaderboard ranks by the **Wilson lower bound** of the
**vanished** rate, descending. The untracked rate is displayed with its own
interval and never contributes to rank position.
**Why Wilson, not the normal approximation:** observed rates here sit near zero
on small samples, exactly where the naive interval produces negative lower bounds
and a zero-width interval at 0 successes — nonsense that would read as certainty.
**Why the lower bound:** a route with 30 trips and 2 vanished shows 6.7%, but the
plausible range runs roughly 1%–22%. Ranking point estimates makes routes trade
places on noise and invites readers to over-interpret the ordering; ranking the
lower bound places a route above another only where the evidence supports it.
**Why vanished only:** ranking on untracked would rank operators by how well
their telematics works, which is not what this project claims to measure.
**Impact:** `aggregate/rates.py` (new), rollups, dataset schema, leaderboard
order. Pinned by a test where lower-bound order and point-estimate order
disagree (2/30 vs 8/200) — otherwise the ranking rule could regress to the point
estimate undetected.

## 2026-07-20 — The 30-trip gate counts trips judged, not trips scheduled
**Decision:** a route is ranked only when `trials = scheduled - excluded >= 30`
in the 28-day window — the same number the board's "Trips judged" column shows
and the same denominator both rates use. The site copy on the index, the route
pages, the methodology and the README all say "trips we could judge" to match.
Below the gate a route is listed with its counts and its intervals, but the
**headline percentage is withheld** and shown as an em dash.
**Why:** gating on `scheduled` would let a route with 30 scheduled and 29
excluded be ranked on a **single observation**: 100% vanished, Wilson lower
bound 0.21, straight to the top of a public list of the worst routes. That is
precisely the failure this project exists to avoid, and the page would have been
claiming a rule the code did not enforce. Withholding the point estimate below
the gate closes the matching contradiction on the route pages, where the index
says no rate is claimed while the detail page printed one.
**Impact:** `publish/site.py` (`leaderboard`, `render_route`), the index and
methodology copy, the README. Pinned by a test with 30 scheduled / 29 excluded
that must land in the unranked list.

## 2026-07-20 — Split trust: the dataset gets its own repository
**Decision:** the published dataset lives in `aleks-drozy/ghost-bus-data`. The
VM holds a fine-grained token scoped to **that repository only**, with
**Contents: Read and write** and nothing else; `ops/publish.sh` builds into a
checkout of it and pushes there, refusing to run if the code checkout is dirty.
Site rendering happens in GitHub Actions, which checks the data repo out beside
the code, builds `_site` from the CSVs, and deploys with `pages: write` +
`id-token: write`. The token reaches git through `GIT_ASKPASS` so it never
enters argv, `ps`, or the journal; `publish.sh` also unsets `GIT_TRACE` and
friends. It lives in `/etc/ghostbus.env` mode 600, the same file and pattern as
`NTA_API_KEY`.
**Why a separate repo, not just a path restriction:** a `Contents: write` token
on the *code* repository can write every non-workflow path, including
`publish/site.py` and the templates — and CI checks that repository out and
executes it. `git add -- data` in a shell script restricts the honest host's
behaviour, not the credential's capability. Scoped to a repository containing
only CSVs and a manifest, the same permission **cannot reach a line of
executable code**. `build_site` also copies only files matching the shapes the
publisher produces (`manifest.json`, `daily|uptime/YYYY-MM-DD.csv`) and aborts
on anything else, so a stray HTML file in the dataset cannot be served from our
own origin.
**Impact:** `ops/publish.sh`, `ops/git-askpass.sh`,
`ops/ghostbus-publisher.{service,timer}`, `.github/workflows/publish.yml`,
`publish/site.py` (`_copy_dataset`, `DatasetError`), RUNBOOK §8.

## 2026-07-20 — The route slug map lives in the published dataset
**Decision:** the route-id → URL-slug map is persisted as `route_slugs` in
`data/manifest.json`, written by the VM alongside the CSVs, **not** beside the
site build. `publish/slugs.py` holds `slugify`/`slug_map` and is shared by
`publish/dataset.py` and `publish/site.py`, so neither imports the other. The
publisher also carries entries forward for route ids that have dropped out of the
current window; the builder takes the map as given and computes a slug only for
an id the map does not carry.
**Why:** the site is built solely by GitHub Actions (see split trust, above),
which checks out into a brand-new `_site` on an ephemeral runner every run. A map
kept with the build output would therefore read back **empty every time** — the
stable-URL guarantee would be inert in production while still passing a test that
rebuilt into one reused output directory inside a single process. The dataset
manifest is checked out with the data, so the builder always sees the real
previous map, and once a route page has a URL that URL is permanent. Carrying
retired ids forward means a link to a withdrawn route keeps resolving to that
route rather than being silently reassigned to a different one.
**Impact:** `publish/slugs.py` (new, shared), `route_slugs` in the manifest
schema, `publish/dataset.py` (`published_slugs`, `read_published_slugs`),
`publish/site.py` (`build_site` reads the dataset's map instead of its own
previous output). Pinned by a test that builds twice into **two different fresh
output directories** and asserts the route URLs are identical; a rebuild into one
reused directory would not have caught this.
**Caught by the pre-flight scan of the plan**, before any of this code was
written — not in review, and not in production.
```

**`KNOWN_ISSUES.md`** — set the frontmatter `updated: 2026-07-20` and insert this section immediately after the `# Ghost Bus Tracker — Known Issues` heading block, above the current top section:

```markdown
## Scoreboard live but route-empty — the 14-day baseline gate (2026-07-20)

**Not a fault; the gate is doing its job.** The site is deployed and the
publisher runs nightly, but `scoreboard_ready` is `false` and `data/daily/` is
empty until 14 complete service days exist. The baseline clock started with the
2026-07-18 deploy, so the first route-level publish is expected **on or after
2026-08-02**, assuming no gap days. Until then the site renders methodology, the
tracker-uptime strip, and "collecting baseline — day N of 14".

Check where it stands with the one-liner in RUNBOOK §8.6 (reads
`scoreboard_ready` / `coverage.complete_days` straight out of the manifest).
Uptime CSVs are exempt from the gate and have been publishing since the first
run — our own downtime is not a claim about an operator.

**Watch for on the first real publish** (none of these can be settled before
route data exists):

- **Route-name coverage.** `manifest.unnamed_routes` lists route ids present in
  outcomes but absent from `gtfs_routes`. A short list is expected; a long one
  means the timetable loader and the classifier disagree about route ids.
- **Slug stability.** Production route ids contain spaces (`03C 120 e a`), so
  filenames are slugified, and the slug map is carried in the *dataset*
  manifest — written by the VM, checked out by CI — so published URLs do not
  move even though the site is rebuilt from scratch on a fresh runner every
  run. The first build with real ids is the first test of that; check
  `route_slugs` in `data/manifest.json` against the route pages actually
  emitted.
- **Ranking sanity.** Confirm the top entries are routes with real sample sizes
  and that the 30-judged-trip gate is visibly separating "ranked" from "not
  enough data yet". A route appearing high with a small "Trips judged" figure
  would mean the gate is reading the wrong number.
- **First-day CI build time and shape.** The site builder is a smoke test in
  itself: it must not raise on real published data, and `_copy_dataset` must
  not trip on anything the publisher writes. The first run with 14 days of CSVs
  is the first time it sees production-shaped input.

**Staleness is still out of scope and still open.** The 2026-07-19 reading
(2.3% of pings older than the classifier's 10-minute COMPLETED window) justifies
*designing* an amendment, and the publisher does not change that: the site says
feed staleness is measured but not yet acted on. That remains its own spec cycle.
```

- [ ] **Step 4: Run test to verify it passes**

Run the PowerShell block from Step 1.

Expected: PASS — prints `PUBLISHER-DOCS-OK` and no `MISSING:` lines.

- [ ] **Step 5: Commit**

The vault is outside the repo and is not version-controlled by it, so there is no commit for this task. Verify the repo is unchanged instead:

```
cd C:\Users\Alex\Projects\ghost-bus
git status --porcelain
```

Expected: empty output — the vault edits touched nothing in the repository. If anything is listed, it belongs to an earlier task and must be committed there before this task is considered done.

Then confirm the whole suite one final time:

```
cd C:\Users\Alex\Projects\ghost-bus; python -m pytest -q
```

Expected: PASS — 0 failed, 0 errors.
