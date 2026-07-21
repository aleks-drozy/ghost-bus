"""CLI-level test for publish/site.py's main(): a deliberate refusal to build
must print a one-line, unmistakable reason and exit 1 - never a raw
traceback that could be mistaken for a runner flake. Mirrors
tests/test_dataset_cli.py's test_cli_gate_failure_exits_nonzero_and_leaves_no_files
for the sibling publish/dataset.py CLI, which established this pattern first.
"""
import subprocess
import sys
from pathlib import Path

from tests.site_fixtures import daily_row, uptime_row, write_dataset

REPO_ROOT = Path(__file__).resolve().parent.parent


def _dataset(tmp_path, **manifest_overrides):
    manifest = {"coverage": {"first_day": "2026-06-28", "last_day": "2026-06-28",
                              "complete_days": 28}}
    manifest.update(manifest_overrides)
    return write_dataset(
        tmp_path / "data",
        daily_rows=[daily_row("2026-06-28", "BIG", scheduled=200, vanished=8,
                               untracked=4, completed=188)],
        uptime_rows=[uptime_row("2026-06-28")],
        manifest=manifest)


def test_cli_refuses_a_dataset_that_fails_its_own_gate_without_a_traceback(tmp_path):
    # scoreboard_ready=False with a daily/ directory present anyway: the
    # cheapest DatasetError trigger in the suite (see test_site_build.py's
    # test_build_site_refuses_route_data_behind_a_pre_baseline_page for the
    # same shape at the build_site() level), and one that fires before
    # out_dir is ever created, so "nothing was written" is also checkable
    # here the way test_dataset_cli.py checks it for the sibling CLI.
    data = _dataset(tmp_path, scoreboard_ready=False)
    out = tmp_path / "_site"
    proc = subprocess.run(
        [sys.executable, "-m", "publish.site", "--data", str(data), "--out", str(out)],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stdout + proc.stderr
    assert "::error::REFUSING TO BUILD" in proc.stdout
    assert not out.exists()


def test_cli_happy_path_still_exits_zero_and_prints_the_build_summary(tmp_path):
    # Guards against the try/except above accidentally swallowing the
    # success path too.
    data = _dataset(tmp_path)
    out = tmp_path / "_site"
    proc = subprocess.run(
        [sys.executable, "-m", "publish.site", "--data", str(data), "--out", str(out)],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "built" in proc.stdout
    assert (out / "index.html").is_file()
