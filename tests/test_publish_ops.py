"""Pins the VM-side publisher: dataset-only pushes, token never exposed.

Assertions run against comment-stripped source: the comments exist to explain
the security properties, and a grep-based test must not be satisfied or defeated
by prose.

Two properties below are pinned behaviourally (running the real ops/publish.sh
under bash against a throwaway code checkout, a throwaway data checkout, and a
local bare repo standing in for GitHub - no network, no real GitHub, no real
VM) rather than by source-text ordering: a static "does string A appear before
string B in the file" check cannot distinguish "the dirty-checkout guard runs
first, on purpose, before the dataset build" from "a gate failure still lets a
push through" - and an earlier version of this file asserted the wrong one.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
OPS = REPO / "ops"
PUBLISH_SH = OPS / "publish.sh"
ASKPASS_SH = OPS / "git-askpass.sh"
SERVICE = OPS / "ghostbus-publisher.service"
TIMER = OPS / "ghostbus-publisher.timer"
CLASSIFIER = OPS / "ghostbus-classifier.service"

TOKEN_VAR = "GHOSTBUS_PUBLISH_TOKEN"


def code(text: str) -> str:
    """Executable lines only."""
    return "\n".join(line for line in text.splitlines()
                     if line.strip() and not line.strip().startswith("#"))


@pytest.fixture(scope="module")
def publish():
    return code(PUBLISH_SH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def askpass():
    return code(ASKPASS_SH.read_text(encoding="utf-8"))


def test_all_publisher_files_exist():
    for path in (PUBLISH_SH, ASKPASS_SH, SERVICE, TIMER):
        assert path.is_file(), f"missing {path}"


def test_publish_script_is_strict_and_never_traces(publish):
    assert "set -euo pipefail" in publish
    # `set -x` would print the whole environment-derived command stream.
    assert "set -x" not in publish
    assert "set +x" in publish


def test_publish_script_never_names_the_token(publish):
    """Only the askpass helper reads the token."""
    assert TOKEN_VAR not in publish


def test_publish_script_disables_git_tracing(publish):
    # A GIT_TRACE left in /etc/ghostbus.env would put the credential exchange
    # in the journal.
    assert "unset GIT_TRACE" in publish


def test_askpass_prints_the_token_and_nothing_else(askpass):
    assert TOKEN_VAR in askpass
    assert 'printf %s "${GHOSTBUS_PUBLISH_TOKEN}"' in askpass
    assert "echo" not in askpass
    assert "set +x" in askpass


def test_dataset_is_pushed_to_its_own_repository(publish):
    # Split trust: a Contents:write token on the CODE repo could rewrite
    # publish/site.py, which CI checks out and executes.
    assert "github.com/aleks-drozy/ghost-bus-data.git" in publish
    assert "ghost-bus.git" not in publish


def test_push_url_carries_no_credential(publish):
    assert "https://x-access-token@github.com/aleks-drozy/ghost-bus-data.git" in publish
    assert "x-access-token:" not in publish, "no token may be embedded in the URL"


def test_uses_the_dataset_cli_flags_that_exist(publish):
    assert "--data-dir" in publish
    assert "--out " not in publish, "publish.dataset takes --data-dir, not --out"
    assert "--commit" not in publish, "the dataset CLI never touches git"


def test_everything_staged_is_staged_by_explicit_path(publish):
    assert "git add -A" not in publish
    assert "git add ." not in publish
    assert "git commit -a" not in publish
    assert "git add -- ." in publish or "git add --" in publish


def test_aborts_when_the_code_checkout_is_dirty(publish):
    assert "git status --porcelain" in publish
    assert "refusing to publish" in publish


def test_no_op_when_the_dataset_did_not_change(publish):
    assert "git diff --cached --quiet" in publish


def test_dirty_checkout_guard_precedes_the_dataset_build(publish):
    """The dirty-checkout guard (`git status --porcelain`, step 1) must run
    before `publish.dataset` (step 2): its entire purpose is refusing to run
    ANY code, including the dataset builder, from a tampered checkout, which
    is worthless if the code has already run by the time it fires. This is
    the opposite requirement of the old (deleted) ordering test, which
    asserted `publish.dataset` had to appear first - that would only be
    satisfiable by moving the guard after the dataset build, defeating it.
    See `test_gate_failure_leaves_the_remote_untouched` for the actual
    security property (gate failure -> nothing pushed), proven behaviourally.
    """
    assert publish.index("git status --porcelain") < publish.index("publish.dataset")


def test_service_matches_the_deployed_layout():
    text = SERVICE.read_text(encoding="utf-8")
    classifier = CLASSIFIER.read_text(encoding="utf-8")
    assert "Type=oneshot" in text
    assert "EnvironmentFile=/etc/ghostbus.env" in text
    assert "EnvironmentFile=/etc/ghostbus.env" in classifier
    assert "WorkingDirectory=/opt/ghost-bus" in text
    assert "WorkingDirectory=/opt/ghost-bus" in classifier
    assert "ExecStart=/opt/ghost-bus/ops/publish.sh" in text


def test_service_runs_after_the_classifier_and_the_network():
    text = SERVICE.read_text(encoding="utf-8")
    assert "ghostbus-classifier.service" in text
    assert "network-online.target" in text


def test_timer_runs_once_a_day_after_the_service_day_closes():
    """Pins the exact daily schedule, then separately rules out repeat/step
    syntax (e.g. the classifier's `*:0/10`, or a date step like `*-*-*/2`)
    creeping into the calendar/time portion. The no-slash check is scoped to
    that portion only: `Europe/Dublin` legitimately contains a '/' as part of
    the IANA zone name, and an earlier version of this test rejected that
    literal value it had just required two lines above - unsatisfiable by
    any implementation. Do not restore the whole-line form of this check.
    """
    text = TIMER.read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 03:30 Europe/Dublin" in text
    assert "Persistent=true" in text
    assert "WantedBy=timers.target" in text
    schedule_line = text.split("OnCalendar=")[1].splitlines()[0]
    date_and_time = schedule_line.rsplit(" ", 1)[0]  # drop " Europe/Dublin"
    assert "/" not in date_and_time, "step syntax (e.g. */2) would fire more than once a day"


# ---------------------------------------------------------------------------
# Behavioural harness: runs the real ops/publish.sh under bash against a
# throwaway "code" checkout, a throwaway "data" checkout, and a local bare
# repo standing in for GitHub. No network, no real GitHub, no real VM.
# ---------------------------------------------------------------------------

def _find_bash() -> str:
    found = shutil.which("bash")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ):
        if os.path.isfile(candidate):
            return candidate
    pytest.fail("no bash interpreter found to run ops/publish.sh behaviourally")


def _posix(path: Path) -> str:
    """A path string bash accepts, whether the interpreter is MSYS or Linux."""
    return str(path).replace("\\", "/")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                    capture_output=True, text=True)


# Stand-in for `publish.dataset`: reacts to two env knobs so tests control
# gate success/failure and the stray-file scenario without touching the real
# dataset builder (Task 9, out of scope here).
_PY_STUB = '''#!/usr/bin/env python
import os
import sys
import pathlib

args = sys.argv[1:]
data_dir = None
for i, a in enumerate(args):
    if a == "--data-dir" and i + 1 < len(args):
        data_dir = args[i + 1]

exit_code = int(os.environ.get("STUB_DATASET_EXIT", "0"))
if exit_code != 0:
    sys.exit(exit_code)

d = pathlib.Path(data_dir)
(d / "daily").mkdir(parents=True, exist_ok=True)
(d / "uptime").mkdir(parents=True, exist_ok=True)
(d / "daily" / "2026-07-20.csv").write_text("route,otp\\n1,0.9\\n")
(d / "uptime" / "2026-07-20.csv").write_text("date,uptime\\n2026-07-20,1.0\\n")
(d / "manifest.json").write_text("{\\"generated\\": \\"2026-07-20\\"}")

if os.environ.get("STUB_WRITE_STRAY"):
    (d / "daily" / "rogue.txt").write_text("not part of the dataset contract")

sys.exit(0)
'''


class Sandbox:
    """A throwaway /opt/ghost-bus (`repo_dir`), a throwaway data-repo
    checkout (`data_repo`), and a local bare repo (`remote`) standing in for
    aleks-drozy/ghost-bus-data. `run()` executes the real ops/publish.sh
    against them via GHOSTBUS_REPO_DIR / GHOSTBUS_DATA_REPO_DIR /
    GHOSTBUS_DATA_REMOTE.
    """

    def __init__(self, tmp_path: Path):
        self.repo_dir = tmp_path / "opt-ghost-bus"
        self.data_repo = tmp_path / "data-repo"
        self.remote = tmp_path / "remote.git"

        (self.repo_dir / ".venv" / "bin").mkdir(parents=True)
        stub = self.repo_dir / ".venv" / "bin" / "python"
        stub.write_text(_PY_STUB)
        stub.chmod(0o755)

        _git(tmp_path, "init", "-q", str(self.repo_dir))
        _git(self.repo_dir, "config", "user.email", "codeowner@example.invalid")
        _git(self.repo_dir, "config", "user.name", "codeowner")
        (self.repo_dir / "README.md").write_text("stand-in code checkout\n")
        # The real deploy has .venv outside version control; match that so
        # the stub interpreter doesn't itself trip the dirty-checkout guard.
        (self.repo_dir / ".gitignore").write_text(".venv/\n")
        _git(self.repo_dir, "add", "README.md", ".gitignore")
        _git(self.repo_dir, "commit", "-q", "-m", "initial")

        _git(tmp_path, "init", "-q", str(self.data_repo))
        _git(tmp_path, "init", "-q", "--bare", str(self.remote))

    def run(self, env_overrides=None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["PATH"] = os.pathsep.join(
            [os.path.dirname(sys.executable), env.get("PATH", "")]
        )
        env["GHOSTBUS_REPO_DIR"] = _posix(self.repo_dir)
        env["GHOSTBUS_DATA_REPO_DIR"] = _posix(self.data_repo)
        env["GHOSTBUS_DATA_REMOTE"] = _posix(self.remote)
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            [_find_bash(), _posix(PUBLISH_SH)],
            cwd=str(self.repo_dir), env=env,
            capture_output=True, text=True,
        )

    def remote_commit_count(self) -> int:
        result = subprocess.run(
            ["git", "--git-dir", str(self.remote), "rev-list", "--all", "--count"],
            capture_output=True, text=True,
        )
        return int(result.stdout.strip() or "0")

    def remote_files(self) -> set:
        # The bare remote's own HEAD symref is whatever init.defaultBranch
        # left it as (often "master"), regardless of which branch was
        # pushed - so name the pushed branch explicitly rather than HEAD.
        result = subprocess.run(
            ["git", "--git-dir", str(self.remote), "show", "--stat",
             "--name-only", "--pretty=format:", "main"],
            capture_output=True, text=True,
        )
        return {line for line in result.stdout.split() if line}


@pytest.fixture()
def sandbox(tmp_path):
    return Sandbox(tmp_path)


def test_gate_failure_leaves_the_remote_untouched(sandbox):
    """The property this task actually cares about: a nonzero exit from
    publish.dataset must push nothing. Proven by running the real
    ops/publish.sh under bash with a stub dataset builder that fails, and
    inspecting a real local bare repo standing in for GitHub - not a
    source-order guess about which string comes first.
    """
    result = sandbox.run(env_overrides={"STUB_DATASET_EXIT": "3"})
    assert result.returncode != 0
    assert sandbox.remote_commit_count() == 0


def test_normal_run_stages_and_pushes_exactly_the_dataset(sandbox):
    result = sandbox.run()
    assert result.returncode == 0, result.stderr
    assert sandbox.remote_commit_count() == 1
    assert sandbox.remote_files() == {
        "data/daily/2026-07-20.csv",
        "data/uptime/2026-07-20.csv",
        "data/manifest.json",
    }


def test_stray_file_in_dataset_aborts_the_publish(sandbox):
    """A non-dataset file appearing inside data/daily (a bug in
    publish.dataset, or an intrusion) must never reach the public repo
    automatically: publish.sh must detect it in the staged set and refuse,
    rather than let `git add` on a directory sweep it in silently.
    """
    result = sandbox.run(env_overrides={"STUB_WRITE_STRAY": "1"})
    assert result.returncode != 0
    assert "refusing to publish" in result.stderr
    assert sandbox.remote_commit_count() == 0
    # The abort also leaves nothing staged behind in the data checkout.
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=str(sandbox.data_repo), capture_output=True, text=True,
    ).stdout.strip()
    assert staged == ""


def test_ops_files_use_unix_line_endings():
    for path in (PUBLISH_SH, ASKPASS_SH, SERVICE, TIMER):
        assert b"\r" not in path.read_bytes(), path.name
