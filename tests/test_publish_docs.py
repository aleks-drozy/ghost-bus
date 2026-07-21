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
