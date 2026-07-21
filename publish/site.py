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
import math
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
