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


def test_about_data_escapes_a_hostile_unnamed_route_id(tmp_path):
    # unnamed_routes is a list of raw route ids straight from the operator's
    # feed (see publish/dataset.py:unnamed_routes) - render_page's content= is
    # unescaped by convention, so this string must be escaped at the render
    # site or a hostile route id becomes live markup on a public page.
    data = write_dataset(tmp_path / "data")
    manifest = dict(DEFAULT_MANIFEST)
    manifest["unnamed_routes"] = ['<script>alert(1)</script>"']
    html = render_about_data(SITE_DIR, manifest, data)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&quot;" in html


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
    assert "Timetable loaded</dt><dd>—</dd>" in html


def test_about_data_null_coverage_renders_em_dashes_not_blanks(tmp_path):
    # An empty database publishes coverage nulls. An absent value must be shown
    # as explicitly unknown, not as an empty gap in a sentence.
    data = write_dataset(tmp_path / "data")
    manifest = dict(DEFAULT_MANIFEST)
    manifest["coverage"] = {"first_day": None, "last_day": None, "complete_days": 0}
    html = render_about_data(SITE_DIR, manifest, data)
    assert "— to —" in html
