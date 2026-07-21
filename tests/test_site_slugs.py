import pytest

from publish.slugs import InvalidSlugError, slug_map, slugify


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


def test_slug_map_rejects_a_path_traversal_slug():
    """route_slugs in manifest.json is read back across a process boundary
    (publish/site.py's build_site interpolates it straight into a filesystem
    path), so a malformed entry here is not a slug to fall back from quietly
    - it means the manifest is corrupt or tampered."""
    with pytest.raises(InvalidSlugError):
        slug_map(["SAFE"], existing={"SAFE": "../../pwned"})


def test_slug_map_rejects_an_absolute_path_slug():
    with pytest.raises(InvalidSlugError):
        slug_map(["SAFE"], existing={"SAFE": "/etc/passwd"})


def test_slug_map_rejects_an_existing_slug_with_disallowed_characters():
    for bad in ("Not Valid!", "has spaces", "UPPERCASE", "", "-leading-hyphen",
                "trailing/slash", "back\\slash", "dot.dot"):
        with pytest.raises(InvalidSlugError):
            slug_map(["SAFE"], existing={"SAFE": bad})


def test_slug_map_accepts_a_well_formed_existing_slug():
    got = slug_map(["SAFE"], existing={"SAFE": "safe-route-2"})
    assert got == {"SAFE": "safe-route-2"}
