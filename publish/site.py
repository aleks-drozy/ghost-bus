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
