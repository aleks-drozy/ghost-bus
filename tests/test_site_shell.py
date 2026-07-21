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
