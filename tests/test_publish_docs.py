"""Pins the operator-facing publishing documentation.

Docs rot silently. These assertions cover the parts an operator would be
harmed by missing: the token's scope and storage rules, the "do nothing"
answer to a gate failure, and the rotation procedure.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RUNBOOK = REPO / "ops" / "RUNBOOK.md"


@pytest.fixture(scope="module")
def runbook():
    return RUNBOOK.read_text(encoding="utf-8")


def section(text, heading):
    """Return the text of a '## ' section, up to the next '## ' heading."""
    start = text.index(heading)
    rest = text[start + len(heading):]
    match = re.search(r"^## ", rest, flags=re.MULTILINE)
    return rest[: match.start()] if match else rest


def test_runbook_has_a_publishing_section(runbook):
    assert "## 8. Publishing" in runbook


def test_publishing_section_covers_install_and_the_daily_timer(runbook):
    body = section(runbook, "## 8. Publishing")
    assert "ghostbus-publisher.timer" in body
    assert "systemctl enable --now ghostbus-publisher.timer" in body
    assert "chmod +x /opt/ghost-bus/ops/publish.sh" in body


def test_publishing_section_states_the_token_scope_and_storage(runbook):
    body = section(runbook, "## 8. Publishing")
    assert "GHOSTBUS_PUBLISH_TOKEN" in body
    assert "/etc/ghostbus.env" in body
    assert "chmod 600 /etc/ghostbus.env" in body
    assert "Contents: Read and write" in body
    assert "ghost-bus-data" in body
    assert "never be echoed" in body


def test_publishing_section_explains_why_the_dataset_has_its_own_repo(runbook):
    body = section(runbook, "## 8. Publishing")
    assert "cannot reach" in body
    assert "publish/site.py" in body


def test_publishing_section_documents_the_pages_source_setting(runbook):
    body = section(runbook, "## 8. Publishing")
    assert "Settings -> Pages" in body
    assert "Source: GitHub Actions" in body


def test_publishing_section_uses_the_real_cli_flags(runbook):
    body = section(runbook, "## 8. Publishing")
    assert "--data-dir" in body
    assert "publish.dataset --db state/ghostbus.db --out " not in body


def test_gate_failure_procedure_says_publish_nothing(runbook):
    body = section(runbook, "## 8. Publishing")
    assert "### 8.4" in body
    assert "Nothing was published" in body
    assert "previously published data stays up" in body
    assert "Do not force a publish" in body


def test_rotation_procedure_is_documented(runbook):
    body = section(runbook, "## 8. Publishing")
    assert "### 8.5 Rotating the publish token" in body
    assert "systemctl restart" in body or "daemon-reload" in body


def test_pre_baseline_mode_is_diagnosable(runbook):
    body = section(runbook, "## 8. Publishing")
    assert "scoreboard_ready" in body
    assert "complete_days" in body


README = REPO / "README.md"


@pytest.fixture(scope="module")
def readme():
    return README.read_text(encoding="utf-8")


def test_readme_has_a_scoreboard_section(readme):
    assert "## The scoreboard & open data" in readme


def test_readme_links_the_site_and_the_dataset(readme):
    body = section(readme, "## The scoreboard & open data")
    assert "https://aleks-drozy.github.io/ghost-bus/" in body
    assert "ghost-bus-data" in body
    assert "daily/" in body
    assert "uptime/" in body
    assert "manifest.json" in body


def test_readme_states_the_baseline_gate(readme):
    body = section(readme, "## The scoreboard & open data")
    assert "14 complete service days" in body


def test_readme_states_the_two_rates_are_never_summed(readme):
    body = section(readme, "## The scoreboard & open data")
    assert "never summed" in body


def test_readme_gate_copy_counts_trips_judged(readme):
    body = section(readme, "## The scoreboard & open data")
    assert "30 trips we could judge" in body
    assert "30 scheduled trips" not in body


def test_readme_publishes_no_reliability_numbers(readme):
    """No percentages: any figure here is stale the next time data lands."""
    body = section(readme, "## The scoreboard & open data")
    assert not re.search(r"\d+(\.\d+)?\s*%", body), "no reliability figures in the README"


def test_readme_tree_lists_the_new_packages(readme):
    assert "publish/" in readme
    assert "site/" in readme


def test_attribution_appears_once(readme):
    assert readme.count("National Transport Authority") == 1
