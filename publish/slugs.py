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
_VALID_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class InvalidSlugError(RuntimeError):
    """Raised when a published route_slugs entry is not a safe, slug-shaped string.

    route_slugs in data/manifest.json is written by the VM's own publisher,
    but the site builder reads it back across a process (and deploy)
    boundary and treats it as untrusted, same as any other manifest field
    (see tests/test_site_escaping.py). publish/site.py's build_site
    interpolates a route's slug directly into a filesystem path
    (out_dir/route/<slug>.html), so an entry like "../../pwned" is not a slug
    to fall back from quietly - slugify never produces one, so seeing one
    means the manifest is corrupt or tampered, and accepting it would let a
    doctored manifest write outside the directory _claim_output_dir exists
    to fence.
    """


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

    Every value in `existing` must match the same [a-z0-9][a-z0-9-]* shape
    slugify produces, or InvalidSlugError is raised: this function's caller
    interpolates the result straight into a filesystem path, so a malformed
    entry (e.g. "../../pwned") halts the build rather than being silently
    reassigned a fresh slug, which would move a published URL without saying
    so.
    """
    ids = sorted(set(route_ids))
    mapping: dict[str, str] = {}
    used: set[str] = set()

    for route_id in ids:
        slug = (existing or {}).get(route_id)
        if slug is not None and not _VALID_SLUG.match(slug):
            raise InvalidSlugError(
                f"existing slug {slug!r} for route id {route_id!r} does not "
                "match the safe filename shape ^[a-z0-9][a-z0-9-]*$")
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
