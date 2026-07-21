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


def test_triggers_on_site_source_changes_and_on_a_daily_schedule(text):
    assert "branches: [main]" in text
    # A change to the builder or a template must redeploy: otherwise the live
    # site keeps serving stale HTML until the data happens to move.
    for path in ("'publish/**'", "'site/**'", "'.github/workflows/publish.yml'"):
        assert path in text, path
    # repository_dispatch was considered and rejected: the only API call that
    # could fire one at this repo ("Create a repository dispatch event")
    # requires a Contents:write token on THIS repo by GitHub's own
    # fine-grained-permission table - exactly the capability spec D4 denies
    # the VM. No credential can send that dispatch without reintroducing what
    # the trust split removed, so this polls on a schedule instead.
    assert "repository_dispatch" not in text
    assert "cron: '37 4 * * *'" in text
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
    # Positional only: text.index finds the first occurrence of each
    # substring anywhere in the file and compares byte offsets. It has no
    # idea which job or step either line belongs to, so it would still pass
    # if the pytest step moved to a step in a job the build step does not
    # depend on. A structural check - parsing the YAML and walking the real
    # step list - would need PyYAML, which the stdlib-only rule excludes (see
    # module docstring). This is a known, accepted gap, not a proof.
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
