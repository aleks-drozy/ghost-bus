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
