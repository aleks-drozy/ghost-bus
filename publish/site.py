"""Build the public scoreboard site from the PUBLISHED CSVs.

This module runs in CI, never on the VM, and never opens the database. Its only
inputs are data/manifest.json, data/daily/*.csv and data/uptime/*.csv, so a
number on the site cannot differ from the number in the downloadable data
(design decision D3). stdlib only (D5): string.Template plus html.escape().
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import re
import shutil
import sys
from pathlib import Path
from string import Template

from aggregate.rates import rate_with_interval
from publish.slugs import slug_map

COUNT_FIELDS = ("scheduled", "excluded", "cancelled", "completed", "vanished", "untracked")
RATE_FIELDS = (
    "vanished_rate", "vanished_lo", "vanished_hi",
    "untracked_rate", "untracked_lo", "untracked_hi",
)

WINDOW_DAYS = 28
MIN_TRIPS = 30

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


def _to_int(value, *, path, column) -> int:
    """A count must be a non-negative integer, or blank (meaning zero).

    These files are machine-written by our own publisher and validated by the
    publish gate before they are written, so a value outside that shape means
    the file is corrupt or truncated. Raising with the file and column named
    is the honest outcome; silently clamping or skipping would let a corrupt
    file render a plausible-looking number on a public accountability page.
    """
    if value in ("", None):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{path}: column '{column}' has a non-integer count {value!r}"
        ) from exc
    if number < 0:
        raise ValueError(f"{path}: column '{column}' has a negative count {value!r}")
    return number


def _to_float(value, *, path, column) -> float | None:
    """Blank means undefined, and undefined is never 0.0 (spec failure table).

    Anything else must be a finite rate in [0.0, 1.0]. As with _to_int, these
    files are machine-written by our own publisher and gate-validated before
    being written, so a value outside that range (e.g. an unbounded exponent
    like "1e999", which float() happily turns into inf with no exception, or a
    rate above 1.0) means the file is corrupt or truncated -- not a rate we
    should render.
    """
    if value in ("", None):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{path}: column '{column}' has a non-numeric rate {value!r}"
        ) from exc
    if not math.isfinite(number) or not (0.0 <= number <= 1.0):
        raise ValueError(
            f"{path}: column '{column}' has a rate outside [0.0, 1.0]: {value!r}"
        )
    return number


def read_manifest(data_dir) -> dict:
    return json.loads((Path(data_dir) / "manifest.json").read_text(encoding="utf-8"))


def read_daily(data_dir) -> list[dict]:
    """Every row of every data/daily/*.csv, oldest file first.

    An absent daily/ directory is not an error: before the 14-day baseline the
    publisher writes none, and that is the documented state of the dataset.

    route_id, route_short_name, route_long_name and agency_name come back as
    raw, unescaped strings straight from GTFS -- external data from the
    transit operator, not from us. This reader does not escape anything; any
    later code that renders one of these four fields into HTML must pass it
    through esc() at the point of rendering.
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
                    row[field] = _to_int(row.get(field), path=path, column=field)
                for field in RATE_FIELDS:
                    row[field] = _to_float(row.get(field), path=path, column=field)
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
                row["expected_minutes"] = _to_int(
                    row.get("expected_minutes"), path=path, column="expected_minutes"
                )
                row["ok_minutes"] = _to_int(
                    row.get("ok_minutes"), path=path, column="ok_minutes"
                )
                row["uptime_fraction"] = _to_float(
                    row.get("uptime_fraction"), path=path, column="uptime_fraction"
                )
                rows.append(row)
    return rows


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
            "rate, worst first — a conservative ordering, not a claim that neighbouring "
            "routes are statistically distinguishable from each other. Untracked is shown "
            "separately and has no effect on position. The two rates are never added "
            'together — <a href="methodology.html">here is why</a>.</p>'
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


def _window_heading(dates: list[str]) -> str:
    """State the count AND the true calendar span behind it, never just the count.

    `window_dates` returns the days actually PRESENT in the data, not a fixed
    trailing range (see its own docstring). A gapped publisher - a VM outage,
    a failed gate, a corrupted file - can make the two disagree by a lot: 28
    judged days can stretch across 40 calendar days. "Last 28 complete
    service days" alone reads as "roughly the last month"; a reader would be
    misled by omission if the evidence actually reached back further than
    that. The index page's window_line states both for the same reason
    (D3/D6 honesty); this mirrors that structure rather than inventing a
    second convention for the same fact.

    Degenerate cases are handled explicitly rather than falling through to a
    grammatically odd sentence: zero days (defensive - the pre-baseline path
    never reaches a real route page, since a route only has an entry at all
    if at least one of its rows fell inside the window) and exactly one day
    (which has no span to state - "day X to day X" would be noise, not
    honesty).
    """
    count = len(dates)
    if count == 0:
        return "No complete service days published yet"
    if count == 1:
        return f"Last 1 complete service day, {dates[0]}"
    return f"Last {count} complete service days, {dates[0]} to {dates[-1]}"


def render_route(site_dir, manifest: dict, entry: dict, daily_rows: list[dict],
                 slugs: dict[str, str], position: int | None = None) -> str:
    ranked_route = position is not None
    if ranked_route:
        rank_line = f"ranked #{position} by the lower bound of the vanished rate"
    else:
        rank_line = ("not ranked — fewer than 30 trips we could judge in the "
                     "window, so no rate is claimed for this route")

    window_heading = _window_heading(window_dates(daily_rows))

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
    agencies = manifest.get("agencies") or []
    agencies_html = (
        "none configured"
        if not agencies
        else ", ".join(esc(name) for name in agencies)
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
        agencies=agencies_html,
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


class DatasetError(RuntimeError):
    """The published dataset is not shaped the way publish/dataset.py writes it."""


class OutputDirError(RuntimeError):
    """out_dir exists, is non-empty, and was not built by this tool.

    build_site deletes and recreates <out_dir>/data and <out_dir>/route on
    every run. Nothing about out_dir proves it is safe to do that to: a typo
    or a careless --out (e.g. "--out ." from the repo root, where out_dir/data
    IS the published dataset) would silently destroy whatever was already
    there. An operator can always empty a directory themselves; they cannot
    un-delete it, so every ambiguous case here raises instead of deleting.
    """


_DATA_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.csv$")
_SENTINEL = ".ghost-bus-site"


def _claim_output_dir(out_dir: Path) -> None:
    """Refuse to build into a directory this tool did not create.

    A directory qualifies if it does not exist yet, is empty, or already
    carries the sentinel this function's caller writes on every successful
    build (a previous build by this same tool, safe to overwrite). Anything
    else - a real project directory, an unrelated non-empty folder, the
    published dataset's own directory - is left untouched and raises.
    """
    if out_dir.exists() and any(out_dir.iterdir()) and not (out_dir / _SENTINEL).is_file():
        raise OutputDirError(
            f"refusing to build into {out_dir}: it already exists, is not "
            f"empty, and has no {_SENTINEL} sentinel, so this tool cannot "
            "tell it apart from a directory it does not own. Point --out at "
            "a fresh or empty directory, or delete this one yourself first.")


_INERT_SUFFIXES = frozenset({".json", ".csv"})


def _write(path: Path, text: str) -> None:
    """Write text to path - the chokepoint every file this module RENDERS
    passes through.

    (style.css is copied, and the dataset and the sentinel are written
    elsewhere; this is not the only way bytes reach out_dir. It is the only
    way rendered text does.)

    assert_inert runs here rather than at each call site inside build_site: a
    future call site that writes a NEW page (bypassing the `pages` dict and
    the two route loops) gets the guard automatically, because it has no
    other way to reach disk. Conventionally remembering to call assert_inert
    at every call site is exactly the discipline-not-enforcement gap this
    task exists to close.

    The gate is an allowlist of INERT suffixes, not a denylist of dangerous
    ones. .htm, .HTML, .xhtml and .svg are all served as markup by common
    hosts, and a denylist fails open for whichever suffix nobody thought of.
    """
    if path.suffix.lower() not in _INERT_SUFFIXES:
        assert_inert(text, str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


class InjectionError(RuntimeError):
    """Raised when a rendered page contains markup we did not put there."""


ALLOWED_TAGS = frozenset({
    "html", "head", "meta", "title", "link", "body",
    "header", "nav", "main", "footer", "section",
    "h1", "h2", "h3", "p", "a", "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "th", "td",
    "dl", "dt", "dd", "span", "strong", "em", "code", "small", "abbr", "br", "hr",
})

# The FULL tag name, not just its leading alphanumeric run: 'a-x' and
# 'a:script' both start with the allowlisted letter 'a', but neither IS 'a' -
# 'a-x' is a distinct custom-element tag name, and browsers still fire
# global event-handler content attributes (onmouseover, etc.) on an unknown
# element. Stopping at the first non-alphanumeric character would let a
# hostile tag slip past the allowlist just by sharing a prefix with a
# legitimate one.
_TAG_NAME = re.compile(r"</?([a-zA-Z][^\s/>]*)")
# Scoped to a single tag's own text (see _TAG_CHUNK below), not the whole
# page: esc() does not touch '=' or ':', so a route legitimately named
# 'href=javascript:alert(1)' renders as those literal characters sitting in
# ordinary text, never inside a real tag. Scanning the whole page for this
# substring would halt the build over inert prose - an availability bug
# triggerable by the same untrusted party this function exists to defend
# against, and exactly the false-positive class already disclaimed below for
# 'x onerror=y'.
# Quote-aware: the HTML5 tokenizer does NOT end a tag at a ">" inside a quoted
# attribute value, so a naive <[^>]*> splits <a title=">" href="javascript:...">
# after the title and never scans the href - while a browser sees one <a> with a
# live handler. The first alternative consumes balanced quoted runs; the second
# is the fallback for a tag with no quotes at all.
_TAG_CHUNK = re.compile(r"""<(?:[^>"']|"[^"]*"|'[^']*')*>|<[^>]*>""")
_JS_HREF = re.compile(r"""(?:href|src)\s*=\s*["']?\s*javascript:""", re.IGNORECASE)


def assert_inert(text: str, source: str = "") -> None:
    """Fail the build if a page carries markup we did not author.

    Externally-sourced strings (route names, agency names, route ids) are
    escaped before templating, so a hostile name reaches the page as
    &lt;script&gt; - text, with no "<" and therefore no tag. If an unescaped one
    ever slips through, it produces a real tag, that tag is not on the
    allowlist, and the build stops rather than shipping it.

    Deliberately a tag-name allowlist and not a general attribute scan: a
    route legitimately named 'x onerror=y' must not fail the build, and once
    escaped it cannot do anything anyway. The field-agnostic verbatim-payload
    test in tests/test_site_escaping.py covers what this cannot see. One
    exception is carved out on purpose: a javascript: URL in an href/src,
    because that can hide inside a tag name this allowlist legitimately
    accepts everywhere (<a>) - see _JS_HREF. This is still not a general
    attribute scan: an ALLOWED tag carrying some other live attribute (e.g.
    <p onmouseover="...">, <meta http-equiv="refresh">) is deliberately not
    caught here; that class of injection requires an unescaped field to reach
    the template in the first place, same as everywhere else this guard
    protects, and is out of scope for a tag-name-and-one-scheme check.
    """
    where = f" in {source}" if source else ""
    for name in _TAG_NAME.findall(text):
        if name.lower() not in ALLOWED_TAGS:
            raise InjectionError(f"disallowed <{name}> tag{where}")
    for chunk in _TAG_CHUNK.findall(text):
        if _JS_HREF.search(chunk):
            raise InjectionError(f"javascript: URL{where}")


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

    out_dir must be fresh, empty, or already ours (see _claim_output_dir):
    this function deletes and recreates <out_dir>/data and <out_dir>/route,
    and will not do that to a directory it cannot prove it created.
    """
    data_dir = Path(data_dir)
    out_dir = Path(out_dir)
    site_dir = Path(site_dir)

    _claim_output_dir(out_dir)

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
        # (slug_map itself now validates any incumbent slug it is fed - see
        # InvalidSlugError - so a doctored route_slugs entry halts here.)
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
        # _write itself enforces assert_inert for every .html file (the
        # chokepoint); no explicit call is needed here.
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

    # Marks out_dir as ours, so the NEXT build (this is what makes rebuilding
    # into the same out_dir on a persistent dev machine work) is recognised as
    # a rebuild rather than refused as a foreign directory.
    (out_dir / _SENTINEL).write_text("", encoding="utf-8")
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
