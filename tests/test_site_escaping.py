"""Security test for D5: externally-sourced strings must render inert.

Route names come from the GTFS feed. We do not control them. This file is the
pin that says so. It also pins the same discipline for manifest-level fields
that are not literally GTFS text (agencies, schema_version, ...): they reach
the same esc()-then-template path and a doctored manifest.json is on the same
untrusted side of the process/deploy boundary as route_slugs (see
hostile_dataset's comment on route_slugs below), so they get teeth too.
"""
import pytest

from tests.site_fixtures import daily_row, uptime_row, write_dataset

from publish.site import InjectionError, assert_inert, build_site
from publish.slugs import InvalidSlugError, slugify

XSS_ID = "<script>alert(1)</script>"
XSS_SHORT = '" onmouseover="alert(2)'
XSS_LONG = "<img src=x onerror=alert(3)>"
XSS_AGENCY = "</table><script src='//evil.example/x.js'></script>"
HOSTILE = (XSS_ID, XSS_SHORT, XSS_LONG, XSS_AGENCY)

# A route whose route_short_name is EMPTY, so route_label() falls back to its
# route_id - unlike every route above, whose route_short_name is always set.
XSS_ID_BARE = "<script>alert(9)</script>"

# A payload for manifest-level fields (agencies, schema_version, ...): fields
# that are not per-route GTFS text, but reach the same esc() call.
XSS_MANIFEST = "<script>alert(7)</script>"


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
        # route_short_name empty, unlike every route above: route_label()
        # falls back to route_id, so THIS is the route whose raw id (once
        # escaped) actually surfaces as visible text on index.html.
        daily_row("2026-06-28", XSS_ID_BARE, scheduled=10, excluded=0,
                  cancelled=0, completed=9, vanished=1, untracked=0,
                  route_short_name="", route_long_name="",
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
                                       "SAFE": "safe",
                                       XSS_ID_BARE: "script-alert-9-script"}})


def all_html(out):
    return {path: path.read_text(encoding="utf-8") for path in out.rglob("*.html")}


def test_no_hostile_string_appears_verbatim_in_any_emitted_file(tmp_path):
    """Payload-agnostic across every PAGE, but field-specific to the four
    payloads HOSTILE actually weaponises (route_id, route_short_name,
    route_long_name, agency_name): it catches a missed esc() on any of THOSE
    four fields wherever they are rendered, including attribute-context
    payloads with no '<' that assert_inert's tag scan cannot see. It proves
    nothing about a field this test does not put a hostile value into - see
    test_hostile_manifest_string_fields_are_escaped for the manifest-level
    fields, and test_hostile_coverage_last_day_crashes_the_build_rather_than_publishing
    for the one field that cannot be weaponised this way at all."""
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
    # This route's short/long names are the OTHER hostile payloads (see
    # hostile_dataset); its route_id is the one place <script>alert(1)</script>
    # itself renders as text, and that is the route detail page
    # (route.html.tmpl's `route_id=esc(entry["route_id"])`), not the index —
    # route_label() prefers route_short_name over route_id whenever the short
    # name is set, which it always is here.
    out = tmp_path / "_site"
    build_site(hostile_dataset(tmp_path), out)
    page = (out / "route" / "script-alert-1-script.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


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


def test_a_hostile_route_id_with_no_short_name_falls_back_and_appears_on_index(tmp_path):
    """route_label() prefers route_short_name and only falls back to
    route_id when the short name is empty. Every other hostile route in this
    fixture sets a non-empty short name, so none of them exercise that
    fallback with a hostile id - this route does, and its escaped id must
    appear right on index.html where the fallback renders it."""
    out = tmp_path / "_site"
    build_site(hostile_dataset(tmp_path), out)
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;alert(9)&lt;/script&gt;" in index


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


def test_assert_inert_rejects_a_custom_element_with_an_allowlisted_prefix():
    """_TAG_NAME must capture the WHOLE tag name, not just its leading
    alphanumeric run: 'a-x' is a distinct (custom-element) tag name from 'a',
    and browsers do fire its event-handler content attributes. Capturing only
    the 'a' prefix would let this sail past the allowlist unnoticed."""
    with pytest.raises(InjectionError):
        assert_inert('<a-x onmouseover="alert(1)">x</a-x>')


def test_assert_inert_rejects_a_namespaced_tag_with_an_allowlisted_prefix():
    with pytest.raises(InjectionError):
        assert_inert("<a:script>x</a:script>")


def test_assert_inert_accepts_inert_text_containing_href_javascript_outside_a_tag():
    """esc() does not touch '=' or ':', so a route legitimately (or hostilely,
    post-escaping) named 'href=javascript:alert(1)' renders as those literal
    characters sitting in ordinary text, never inside a real <...> tag. The
    javascript: check must be scoped to actual tag content, or this harmless
    text halts the whole build - an availability bug triggerable by the same
    untrusted party, and exactly the false-positive class this function's own
    docstring already disclaims for 'x onerror=y'."""
    assert_inert("<p>href=javascript:alert(1)</p>")
    assert_inert("<td>x onerror=y href=javascript:z</td>")


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


def test_write_itself_is_the_enforcement_chokepoint(tmp_path):
    """assert_inert must be enforced INSIDE _write, not only at the three
    call sites inside build_site that currently happen to invoke it. Every
    byte this module ever writes to an .html file passes through _write; a
    future call site that writes one directly (bypassing the `pages` dict and
    the two route loops) must be caught automatically, not conventionally -
    that is the entire point of a chokepoint over three separately-editable
    call sites."""
    import publish.site as site

    with pytest.raises(InjectionError):
        site._write(tmp_path / "new-page.html", "<p>ok</p><script>x</script>")


def _one_safe_route_dataset(tmp_path, *, manifest_overrides=None, route_short_name="7"):
    manifest = {"coverage": {"first_day": "2026-06-28", "last_day": "2026-06-28",
                              "complete_days": 28}}
    if manifest_overrides:
        manifest.update(manifest_overrides)
    return write_dataset(
        tmp_path / "data",
        daily_rows=[daily_row("2026-06-28", "SAFE", scheduled=50, excluded=0,
                              cancelled=0, completed=48, vanished=1, untracked=1,
                              route_short_name=route_short_name,
                              route_long_name="Safe Road",
                              agency_name="Fixtureville Bus")],
        uptime_rows=[uptime_row("2026-06-28")],
        manifest=manifest)


def test_build_site_rejects_a_path_traversal_route_slug_and_writes_nothing_outside_out(tmp_path):
    """A manifest is written by the VM's publisher, but build_site reads it
    back across a process/deploy boundary and cannot prove it was not
    tampered with. route_slugs is interpolated straight into a filesystem
    path (out_dir/route/<slug>.html), so a malformed entry here must not
    reach the filesystem at all - not even inside out_dir."""
    data_dir = _one_safe_route_dataset(
        tmp_path, manifest_overrides={"route_slugs": {"SAFE": "../../pwned"}})
    out = tmp_path / "_site"
    with pytest.raises(InvalidSlugError):
        build_site(data_dir, out)
    # Nothing was written anywhere: slug_map raises before out_dir is even
    # created (build_site computes slugs before out_dir.mkdir()).
    assert not out.exists()
    for path in tmp_path.rglob("pwned.html"):
        pytest.fail(f"traversal payload escaped to {path}")


def test_hostile_manifest_string_fields_are_escaped_on_about_data_page(tmp_path):
    """Manifest-level strings that are not per-route GTFS text - agencies is
    a configured operator allow-list (about-data.html.tmpl says so itself:
    "a configuration setting, not something derived from the feed"), and
    schema_version/generated_at/timetable_hash/timetable_loaded_at are our own
    pipeline metadata - still reach the same esc()-then-template path in
    render_about_data, and manifest.json is read back across the same
    process/deploy boundary as route_slugs. Nothing exercised any of them
    with a hostile value before this test; each is escaped in the existing
    code (this is a coverage fix, not a code fix)."""
    data_dir = _one_safe_route_dataset(tmp_path, manifest_overrides={
        "agencies": [XSS_MANIFEST],
        "schema_version": XSS_MANIFEST,
        "generated_at": XSS_MANIFEST,
        "timetable_hash": XSS_MANIFEST,
        "timetable_loaded_at": XSS_MANIFEST,
        "coverage": {"first_day": XSS_MANIFEST, "last_day": "2026-06-28",
                     "complete_days": 28},
    })
    out = tmp_path / "_site"
    build_site(data_dir, out)
    for path, text in all_html(out).items():
        assert XSS_MANIFEST not in text, f"{XSS_MANIFEST!r} unescaped in {path}"
    about = (out / "about-data.html").read_text(encoding="utf-8")
    escaped = "&lt;script&gt;alert(7)&lt;/script&gt;"
    # One occurrence each for agencies, schema_version, timetable_hash,
    # timetable_loaded_at and coverage_first (5), plus generated_at twice
    # (once in the about-data content itself, once in every page's shared
    # footer) = 7.
    assert about.count(escaped) == 7


def test_hostile_coverage_last_day_crashes_the_build_rather_than_publishing(tmp_path):
    """coverage.last_day is the one about-data field that genuinely cannot be
    weaponised the way the other five are: it also feeds _strip_end ->
    render_uptime_strip's dt.date.fromisoformat, evaluated while building
    index.html, which runs before about-data.html in build_site's `pages`
    dict. A hostile non-date value here cannot reach the about-data renderer
    to be unescaped onto the page - it can only crash the whole build first.
    That is the same safe-failure-over-silent-corruption posture this
    project already uses for malformed input elsewhere (DatasetError,
    _to_int/_to_float), not an escaping gap - documented here rather than
    left as an unexplained gap in the field-agnostic test's coverage."""
    data_dir = _one_safe_route_dataset(tmp_path, manifest_overrides={
        "coverage": {"first_day": "2026-06-28", "last_day": XSS_MANIFEST,
                     "complete_days": 28},
    })
    with pytest.raises(ValueError):
        build_site(data_dir, tmp_path / "_site")


def test_build_site_does_not_halt_for_a_route_legitimately_named_with_javascript_text(tmp_path):
    """End-to-end version of test_assert_inert_accepts_inert_text_containing_
    href_javascript_outside_a_tag: a route whose short name is literally
    'href=javascript:alert(1)' must publish successfully, not halt the build,
    because that text renders as inert prose, never as a live attribute."""
    data_dir = _one_safe_route_dataset(
        tmp_path, route_short_name="href=javascript:alert(1)")
    out = tmp_path / "_site"
    build_site(data_dir, out)  # must not raise
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "href=javascript:alert(1)" in index


# --- the tag chunker must agree with a browser about where a tag ends -------

@pytest.mark.parametrize("markup", [
    '<a title=">" href="javascript:alert(1)">x</a>',
    "<a title='>' href='javascript:alert(1)'>x</a>",
    '<a class="x>y" href="javascript:alert(1)">x</a>',
])
def test_a_quoted_angle_bracket_cannot_hide_a_javascript_url(markup):
    """The HTML5 tokenizer does NOT end a tag at a ">" inside a quoted
    attribute value, so a naive <[^>]*> chunker splits before the href and
    never scans it - while a browser sees one <a> with a live handler.

    This was a real regression: the earlier whole-page scan caught these, and
    scoping the check to tag chunks silently gave up the catch. Do not
    "simplify" _TAG_CHUNK back to <[^>]*>.
    """
    with pytest.raises(InjectionError):
        assert_inert(markup)


def test_ordinary_quoted_attributes_still_pass():
    """The quote-aware chunker must not become a false-positive machine."""
    assert_inert('<td class="num" title="1 of 2">3</td>')
    assert_inert('<a href="../index.html">Back</a>')
    assert_inert("<p>plain</p>")


# --- the write gate is an allowlist of INERT suffixes, not a denylist -------

@pytest.mark.parametrize("name", ["page.html", "page.htm", "page.HTML",
                                  "feed.xhtml", "icon.svg", "page.unexpected"])
def test_live_markup_is_blocked_under_every_non_inert_suffix(tmp_path, name):
    """.htm/.HTML/.xhtml are served as markup by common hosts and .svg can
    carry <script>. Gating on == ".html" let all of them through. The gate is
    an allowlist of inert suffixes so it fails CLOSED for a suffix nobody
    anticipated - which is the whole point of a chokepoint."""
    from publish.site import _write
    with pytest.raises(InjectionError):
        _write(tmp_path / name, "<p>ok</p><script>alert(1)</script>")
    assert not (tmp_path / name).exists()


@pytest.mark.parametrize("name", ["data.json", "data.csv"])
def test_inert_data_files_are_written_unscanned(tmp_path, name):
    """The published dataset legitimately contains raw operator text - it is
    served as data, not markup, and scanning it would fail the build on a
    route name we are required to publish verbatim."""
    from publish.site import _write
    _write(tmp_path / name, '{"route": "<script>alert(1)</script>"}')
    assert (tmp_path / name).exists()
