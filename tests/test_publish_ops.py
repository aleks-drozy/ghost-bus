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
POLLER = OPS / "ghostbus-poller.service"

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


def test_publish_script_never_reads_the_token(publish):
    """publish.sh may NAME the token - it strips it from publish.dataset's
    environment via `env -u GHOSTBUS_PUBLISH_TOKEN` (I5: that component is
    deliberately kept git-free, but nothing stops it reading os.environ, and
    it writes into the directory that gets published) - but must never READ
    its value. Only the askpass helper does that. An earlier version of this
    test required the token's name to be wholly absent, which `env -u` (a
    security improvement, not a leak) directly contradicts; the actual
    property is "never expanded", not "never mentioned".

    NEW-5: checking only the braced form `${GHOSTBUS_PUBLISH_TOKEN}` would
    have let the unbraced expansion `$GHOSTBUS_PUBLISH_TOKEN` (bash treats
    both identically) straight through undetected. Both are checked.
    """
    assert "${GHOSTBUS_PUBLISH_TOKEN}" not in publish
    assert "$GHOSTBUS_PUBLISH_TOKEN" not in publish
    assert "env -u GHOSTBUS_PUBLISH_TOKEN" in publish


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


def test_network_git_commands_disable_credential_helper_persistence(publish):
    """C2: GIT_ASKPASS only answers git's password prompt for this
    invocation - it does not stop git from also handing a successful
    credential to every configured credential.helper's `store` action
    afterward (credential_approve() runs regardless of where the credential
    came from, or which account the process runs as - originally found when
    the unit ran as root with no User=; NEW-2 later added User=ubuntu, which
    changes WHICH account's config matters but not whether an unneutralised
    helper would still fire). A credential.helper configured in
    /etc/gitconfig (system scope) or the running account's own ~/.gitconfig
    would otherwise write the token in plaintext to ~/.git-credentials on
    the first successful push. GIT_CONFIG_NOSYSTEM removes /etc/gitconfig
    from consideration entirely; `-c credential.helper=` (empty resets the
    helper list) is applied directly to fetch and push.
    """
    assert "GIT_CONFIG_NOSYSTEM=1" in publish
    assert publish.count("-c credential.helper=") >= 2


def test_establishes_a_known_base_before_pushing(publish):
    """C1: `git push HEAD:main` pushes HEAD and every ancestor not already on
    the remote. Without fetching and resetting to the remote's actual tip
    first, anything ever committed locally in the data checkout - a person
    debugging, or an attacker with write access there - rides out on the
    next successful publish unexamined. See
    test_unrelated_local_commit_never_reaches_the_remote for the behavioural
    proof.
    """
    assert "git -c credential.helper= fetch" in publish
    assert "git reset --hard FETCH_HEAD" in publish
    assert "git rev-list --count" in publish


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


def test_service_runs_as_ubuntu_not_root():
    """NEW-2: /opt/ghost-bus is chowned to ubuntu at install time (RUNBOOK
    2); running the publisher as ubuntu rather than the systemd default of
    root makes the checkout's owner match the process's UID (so git's
    "dubious ownership" check cannot fire, without needing a safe.directory
    workaround - which GIT_CONFIG_NOSYSTEM in publish.sh would block from
    system scope anyway) and shrinks the blast radius of the one credential
    on this VM able to write to GitHub. EnvironmentFile is read by systemd
    itself before the process starts, so this doesn't require /etc/ghostbus.env
    to be readable by ubuntu.
    """
    text = SERVICE.read_text(encoding="utf-8")
    assert "User=ubuntu" in text


def test_all_three_ghostbus_units_run_as_the_same_user():
    """NEW-6: a reviewer argued (without reproducing it) that User=ubuntu on
    the publisher alone would break its read access to the poller/
    classifier's root-owned WAL-mode SQLite files. A live test found that
    premise empirically false - but resting the one component with a
    repo-write credential on an unexplained cross-UID access pattern was
    judged not worth it regardless of whether it currently happens to work.
    All three units now share one owner, deliberately, so nobody "fixes" one
    of them back to root and reintroduces the question.
    """
    for path in (SERVICE, CLASSIFIER, POLLER):
        text = path.read_text(encoding="utf-8")
        assert "User=ubuntu" in text, path.name


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


# Stand-in for `publish.dataset`: reacts to env knobs so tests control gate
# success/failure, the stray-file scenario, the pre-baseline shape, and
# dataset content across repeated runs, without touching the real dataset
# builder (Task 9, out of scope here). Also doubles as the I5 regression
# sentinel: if the token is still in this process's environment, publish.sh
# failed to strip it, and the stub exits 42 rather than pretending to work.
_PY_STUB = '''#!/usr/bin/env python
import os
import sys
import pathlib

if "GHOSTBUS_PUBLISH_TOKEN" in os.environ:
    sys.exit(42)

args = sys.argv[1:]
data_dir = None
for i, a in enumerate(args):
    if a == "--data-dir" and i + 1 < len(args):
        data_dir = args[i + 1]

exit_code = int(os.environ.get("STUB_DATASET_EXIT", "0"))
if exit_code != 0:
    sys.exit(exit_code)

date = os.environ.get("STUB_DATE", "2026-07-20")
d = pathlib.Path(data_dir)

if os.environ.get("STUB_WITHDRAW_DAILY"):
    # Mirrors publish/dataset.py:393-397 exactly: an ACTIVE rmtree, not
    # merely skipping creation - the NEW-1 regression needs data/daily to
    # have existed (restored by publish.sh's own fetch+reset from a prior
    # publish) and then be removed by this run, not simply never created.
    import shutil as _shutil
    _shutil.rmtree(d / "daily", ignore_errors=True)
elif not os.environ.get("STUB_SKIP_DAILY"):
    (d / "daily").mkdir(parents=True, exist_ok=True)
    (d / "daily" / (date + ".csv")).write_text("route,otp\\n1,0.9\\n")

(d / "uptime").mkdir(parents=True, exist_ok=True)
(d / "uptime" / (date + ".csv")).write_text("date,uptime\\n" + date + ",1.0\\n")
(d / "manifest.json").write_text('{"generated": "' + date + '"}')

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

    `nested_data_repo=True` exercises the actual production default instead
    of overriding every knob (I6): DATA_REPO is left to resolve as
    `${REPO_DIR}/data-repo` (I1's chosen fix - gitignored, like .venv/ -
    rather than a sibling path, precisely so this default stays derived from
    REPO_DIR and testable at all).
    """

    def __init__(self, tmp_path: Path, nested_data_repo: bool = False):
        self.repo_dir = tmp_path / "opt-ghost-bus"
        self.nested_data_repo = nested_data_repo
        self.data_repo = (
            (self.repo_dir / "data-repo") if nested_data_repo else (tmp_path / "data-repo")
        )
        self.remote = tmp_path / "remote.git"

        (self.repo_dir / ".venv" / "bin").mkdir(parents=True)
        stub = self.repo_dir / ".venv" / "bin" / "python"
        stub.write_text(_PY_STUB)
        stub.chmod(0o755)

        _git(tmp_path, "init", "-q", str(self.repo_dir))
        _git(self.repo_dir, "config", "user.email", "codeowner@example.invalid")
        _git(self.repo_dir, "config", "user.name", "codeowner")
        # newline="\n": GIT_CONFIG_NOSYSTEM=1 (exported by publish.sh, see
        # C2) means publish.sh's OWN `git status` does not apply this
        # machine's system-scope core.autocrlf - so if these were written
        # with platform-default (CRLF-on-Windows) newlines while the initial
        # commit below (a plain `git` call, still subject to autocrlf) stored
        # them LF-normalized, publish.sh's dirty-checkout guard would find a
        # spurious byte-level difference and refuse every run. Match the
        # real Linux deploy target, which has no such mismatch to begin with.
        (self.repo_dir / "README.md").write_text("stand-in code checkout\n", newline="\n")
        # The real deploy has .venv (and, nested, data-repo) outside version
        # control; match that so neither trips the dirty-checkout guard.
        ignored = [".venv/"] + (["data-repo/"] if nested_data_repo else [])
        (self.repo_dir / ".gitignore").write_text("\n".join(ignored) + "\n", newline="\n")
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
        if not self.nested_data_repo:
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

    def remote_files(self, ref: str = "main") -> set:
        # The bare remote's own HEAD symref is whatever init.defaultBranch
        # left it as (often "master"), regardless of which branch was
        # pushed - so name the pushed branch explicitly rather than HEAD.
        result = subprocess.run(
            ["git", "--git-dir", str(self.remote), "show", "--stat",
             "--name-only", "--pretty=format:", ref],
            capture_output=True, text=True,
        )
        return {line for line in result.stdout.split() if line}

    def remote_tree_files(self, ref: str = "main") -> set:
        """Every path present in ref's actual tree right now - not just what
        changed in the latest commit's diff. `remote_files()` (git show
        --stat) is only equivalent to "the whole published tree" for a
        first-ever commit; for anything after that it only shows this
        commit's delta, which is the wrong question for "is this path truly
        gone from what's published" (NEW-1).
        """
        result = subprocess.run(
            ["git", "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", ref],
            capture_output=True, text=True,
        )
        return {line for line in result.stdout.split() if line}

    def remote_all_history_files(self) -> set:
        """Every path that has ever appeared in any commit ever pushed - to
        prove a file never rode along on ANY push, not just the latest.
        """
        result = subprocess.run(
            ["git", "--git-dir", str(self.remote), "log", "--all",
             "--name-only", "--pretty=format:"],
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


def test_dataset_builder_never_sees_the_publish_token(sandbox):
    """I5: publish.dataset is deliberately kept git-free and AST-pinned
    against spawning a process, but nothing stops it reading os.environ, and
    it writes into the directory that gets published - a single os.environ
    dump into the manifest would publish the token. The stub exits 42 if it
    still sees the token, so a returncode of 0 here (not 42) is the proof
    publish.sh stripped it via `env -u` before this invocation.
    """
    result = sandbox.run(env_overrides={"GHOSTBUS_PUBLISH_TOKEN": "shhh-do-not-leak"})
    assert result.returncode == 0, result.stderr


def test_pre_baseline_shape_publishes_uptime_and_manifest_only(sandbox):
    """I2: for the first ~14 complete service days, publish.dataset writes
    no data/daily at all (the baseline gate isn't met), but spec D6 keeps
    uptime + manifest exempt so they publish from day one. `git add` is
    fatal on an unmatched pathspec, so the original explicit three-path add
    died at exit 128 every night until baseline - reproduced directly:
    `git add -- data/daily data/uptime data/manifest.json` in a directory
    where only data/uptime and data/manifest.json exist fails with
    "fatal: pathspec 'data/daily' did not match any files". Assert the
    tolerant staging still succeeds and pushes exactly what exists.
    """
    result = sandbox.run(env_overrides={"STUB_SKIP_DAILY": "1"})
    assert result.returncode == 0, result.stderr
    assert sandbox.remote_commit_count() == 1
    assert sandbox.remote_files() == {"data/uptime/2026-07-20.csv", "data/manifest.json"}


def test_withdrawn_daily_data_disappears_from_the_remote(sandbox):
    """NEW-1 (blocker): publish/dataset.py:393-397 rmtree's data/daily when
    coverage falls back below the 14-day baseline - spec D6's WITHDRAWAL
    rule, that previously published route data must come down rather than
    sit next to a page saying we publish nothing about any route. The
    `[ -e ]`-only staging guard (I2's fix) skips a path that no longer
    exists, so the DELETION was never staged: withdrawn CSVs stayed live on
    the public repo forever, and the bug was self-perpetuating - each run's
    fetch+reset restores data/daily from the remote, the builder deletes it
    again, and the guard skips it again.

    Publish a post-baseline dataset (data/daily present), then run again
    with the stub withdrawing data/daily exactly as dataset.py does
    (STUB_WITHDRAW_DAILY - an active rmtree, not merely skipping creation),
    and confirm the remote's actual tree - not just the latest commit's
    diff - no longer carries any data/daily/ entry at all.
    """
    first = sandbox.run(env_overrides={"STUB_DATE": "2026-07-20"})
    assert first.returncode == 0, first.stderr
    assert sandbox.remote_tree_files() == {
        "data/daily/2026-07-20.csv",
        "data/uptime/2026-07-20.csv",
        "data/manifest.json",
    }

    second = sandbox.run(env_overrides={"STUB_DATE": "2026-07-21", "STUB_WITHDRAW_DAILY": "1"})
    assert second.returncode == 0, second.stderr
    assert sandbox.remote_commit_count() == 2

    # The property NEW-1 is about: data/daily must be entirely gone. (Not
    # asserting the exact uptime file set here - the stub writes one
    # date-named uptime CSV per run and never prunes old ones, which isn't
    # how the real rolling uptime window behaves; that's a stub simplification,
    # not a publish.sh staging property.)
    tree_now = sandbox.remote_tree_files()
    assert not any(f.startswith("data/daily/") for f in tree_now), tree_now
    assert "data/manifest.json" in tree_now


def test_pre_baseline_shape_still_publishes_after_the_withdrawal_fix(sandbox):
    """NEW-1's fix (staging a path that's merely tracked-but-missing, via
    `git ls-files`) must not undo I2: a path that was NEVER tracked to begin
    with (true pre-baseline - data/daily has never existed in this
    checkout's history at all) must still be skipped, not staged as a
    phantom deletion. `git ls-files -- data/daily` on a path with no history
    returns nothing either, so the two guards agree here.
    """
    result = sandbox.run(env_overrides={"STUB_SKIP_DAILY": "1"})
    assert result.returncode == 0, result.stderr
    assert sandbox.remote_tree_files() == {"data/uptime/2026-07-20.csv", "data/manifest.json"}


def test_gitignore_excludes_the_nested_data_repo_checkout():
    """I1: DATA_REPO defaults to `${REPO_DIR}/data-repo`, inside the very
    checkout the dirty-checkout guard inspects. Without this entry,
    `git status --porcelain` reports `?? data-repo/` and the guard fires on
    EVERY run - reproduced exactly that way by the reviewer. This is the
    actual production fix; the Sandbox harness in this file builds its own
    synthetic .gitignore regardless (see test_default_data_repo_path_
    resolution below), so only a direct read of the real file catches this
    entry going missing.
    """
    gitignore = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert "data-repo/" in gitignore.splitlines()


def test_default_data_repo_path_resolution(tmp_path):
    """I6: exercise the production default path resolution (DATA_REPO
    derived from REPO_DIR as `${REPO_DIR}/data-repo`) instead of always
    overriding GHOSTBUS_DATA_REPO_DIR - which is exactly how I1 (the guard
    firing on every run because data-repo/ was untracked) passed a green
    suite the first time around. This Sandbox always gitignores data-repo/
    in its own synthetic checkout regardless of the real .gitignore; see
    test_gitignore_excludes_the_nested_data_repo_checkout for that half.
    """
    sandbox = Sandbox(tmp_path, nested_data_repo=True)
    result = sandbox.run()
    assert result.returncode == 0, result.stderr
    assert "code checkout is dirty" not in result.stderr
    assert sandbox.remote_commit_count() == 1
    assert sandbox.remote_files() == {
        "data/daily/2026-07-20.csv",
        "data/uptime/2026-07-20.csv",
        "data/manifest.json",
    }


def test_unrelated_local_commit_never_reaches_the_remote(sandbox):
    """C1: `git push HEAD:main` publishes HEAD and every ancestor not
    already on the remote. Establish a real remote base (a prior legitimate
    publish), commit an unrelated file locally on top of it - a person
    debugging, or an attacker with write access to data-repo - and confirm
    publish.sh's fetch+reset-to-remote step discards that commit before
    staging anything, so only the new dataset commit is ever pushed.
    """
    first = sandbox.run(env_overrides={"STUB_DATE": "2026-07-20"})
    assert first.returncode == 0, first.stderr
    assert sandbox.remote_commit_count() == 1

    (sandbox.data_repo / "evil.html").write_text("<script>pwned</script>")
    _git(sandbox.data_repo, "add", "evil.html")
    _git(sandbox.data_repo, "commit", "-q", "-m", "debugging, definitely not malicious")

    second = sandbox.run(env_overrides={"STUB_DATE": "2026-07-21"})
    assert second.returncode == 0, second.stderr
    assert sandbox.remote_commit_count() == 2
    assert sandbox.remote_files() == {
        "data/daily/2026-07-21.csv",
        "data/uptime/2026-07-21.csv",
        "data/manifest.json",
    }
    assert "evil.html" not in sandbox.remote_all_history_files()


def test_untracked_file_elsewhere_in_the_data_checkout_is_never_staged(sandbox):
    """I6: the stray-file abort gate (test_stray_file_in_dataset_aborts_the_
    publish) only covers a file inside data/daily. A file elsewhere in the
    data checkout - never named by the tolerant `git add` loop at all - must
    equally never be staged or pushed, without needing the abort gate to
    catch it.
    """
    (sandbox.data_repo / "rogue-root-file.txt").write_text("not part of the dataset")
    result = sandbox.run()
    assert result.returncode == 0, result.stderr
    assert "rogue-root-file.txt" not in sandbox.remote_files()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(sandbox.data_repo), capture_output=True, text=True,
    ).stdout
    assert "rogue-root-file.txt" in status, "should still be untracked, not staged"


def test_dirty_guard_fails_closed_when_git_status_itself_fails(sandbox):
    """I3: `if [ -n "$(git status --porcelain)" ]` swallows a FAILING git
    status - `set -e` is suppressed inside a condition, and the
    substitution's own exit status is discarded there regardless - so a
    failure looked "clean" and let a run straight past the guard. Reproduced
    by deleting REPO_DIR's own `.git` so `git status` itself errors, while
    leaving DATA_REPO and the remote fully valid - so if the guard fails
    open, nothing else stops a complete, successful publish. Real triggers
    in production: a stale index.lock, or git missing from ubuntu's PATH
    (NEW-2's User=ubuntu means the checkout-ownership mismatch that used to
    make "dubious ownership" a likely trigger here shouldn't occur anymore,
    but the guard must still fail closed for whatever reason git status
    fails, not just that one).
    """
    def _force_remove(func, path, exc_info):
        # Git marks object files read-only on Windows; retry after clearing it.
        os.chmod(path, 0o700)
        func(path)

    shutil.rmtree(sandbox.repo_dir / ".git", onerror=_force_remove)

    result = sandbox.run()

    assert result.returncode != 0
    # Dies at `git status` itself (set -e), not our own dirty-checkout
    # message - proving the failure is caught, not coincidentally masked by
    # something failing further down the script.
    assert "code checkout is dirty" not in result.stderr
    assert "not a git repository" in result.stderr
    # The property that actually matters: with a valid DATA_REPO and remote
    # sitting right there, a fail-open guard would let a complete, genuine
    # publish through. It must not.
    assert sandbox.remote_commit_count() == 0


def test_askpass_refuses_hosts_other_than_github():
    """M1 (escalated from Minor to blocker): a bare substring test
    (`*github.com*`) is defeated by any of three values, all reproduced end
    to end by the reviewer against a live git exchange - the third actually
    delivered the token to a localhost server:
      - a lookalike hostname:  'https://github.com.evil.example'
      - a query string mention: 'https://evil.example/?ref=github.com'
      - github.com as fake userinfo ahead of the real host:
        'http://github.com@127.0.0.1:8731/...' (git's prompt for this is
        "...for 'http://github.com@127.0.0.1:8731': ")
    The anchored match (host must be the last thing before the closing
    quote) rejects all three and still accepts the two legitimate forms
    (with and without an embedded username).
    """
    env = os.environ.copy()
    env["GHOSTBUS_PUBLISH_TOKEN"] = "shhh"

    def ask(prompt):
        return subprocess.run(
            [_find_bash(), _posix(ASKPASS_SH), prompt],
            env=env, capture_output=True, text=True,
        )

    defeating_prompts = [
        "Password for 'https://github.com.evil.example': ",
        "Password for 'https://evil.example/?ref=github.com': ",
        "Password for 'http://github.com@127.0.0.1:8731': ",
    ]
    for prompt in defeating_prompts:
        result = ask(prompt)
        assert result.returncode != 0, prompt
        assert result.stdout == "", prompt

    legitimate_prompts = [
        "Password for 'https://x-access-token@github.com': ",
        "Username for 'https://github.com': ",
    ]
    for prompt in legitimate_prompts:
        result = ask(prompt)
        assert result.returncode == 0, prompt
        assert result.stdout == "shhh", prompt


def test_ops_files_use_unix_line_endings():
    for path in (PUBLISH_SH, ASKPASS_SH, SERVICE, TIMER):
        assert b"\r" not in path.read_bytes(), path.name
