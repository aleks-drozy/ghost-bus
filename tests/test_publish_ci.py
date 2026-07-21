"""Pins the publish workflow: gates, permissions, ordering, action versions.

Text assertions, not YAML parsing - the project is stdlib-only and PyYAML is
not a dev dependency. What matters is that specific lines exist and that the
test step precedes the build step.
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "publish.yml"


@pytest.fixture(scope="module")
def text():
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_file_exists():
    assert WORKFLOW.is_file(), f"missing workflow: {WORKFLOW}"


def test_triggers_on_site_source_changes_and_on_new_data(text):
    assert "branches: [main]" in text
    # A change to the builder or a template must redeploy: otherwise the live
    # site keeps serving stale HTML until the data happens to move.
    for path in ("'publish/**'", "'site/**'", "'.github/workflows/publish.yml'"):
        assert path in text, path
    # The dataset lives in another repo, so it signals by dispatch.
    assert "repository_dispatch:" in text
    assert "types: [dataset-published]" in text
    assert "workflow_dispatch:" in text


def test_has_pages_permissions_and_no_write_all(text):
    assert "contents: read" in text
    assert "pages: write" in text
    assert "id-token: write" in text
    assert "permissions: write-all" not in text
    assert "contents: write" not in text


def test_has_a_serialising_concurrency_group(text):
    assert "concurrency:" in text
    assert "group: pages" in text
    assert "cancel-in-progress: false" in text


def test_checks_out_the_dataset_repository_into_the_data_directory(text):
    assert "repository: aleks-drozy/ghost-bus-data" in text
    assert "path: data" in text


def test_full_suite_runs_before_the_site_is_built(text):
    suite = text.index("run: python -m pytest")
    build = text.index("run: python -m publish.site")
    assert suite < build, "the test suite must run before the site is built"


def test_builds_the_site_from_the_published_csvs_only(text):
    assert "run: python -m publish.site --data data/data --out _site" in text
    # D3: the site is never built from the database.
    assert "ghostbus.db" not in text
    assert "publish.dataset" not in text


def test_uses_pinned_pages_actions(text):
    for action in (
        "actions/checkout@v5",
        "actions/setup-python@v6",
        "actions/configure-pages@v5",
        "actions/upload-pages-artifact@v3",
        "actions/deploy-pages@v4",
    ):
        assert action in text, f"expected {action}"


def test_setup_python_matches_the_tests_workflow(text):
    assert "python-version: '3.12'" in text
    assert "cache: pip" in text
    assert "pip install -r requirements-dev.txt" in text


def test_deploy_job_depends_on_build_and_targets_the_pages_environment(text):
    assert "needs: build" in text
    assert "name: github-pages" in text
    assert "url: ${{ steps.deployment.outputs.page_url }}" in text
