import ast
import datetime as dt
import inspect
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import publish.dataset as dataset
from publish.dataset import GateFailed, write_dataset
from tests.dataset_fixture import build_db, consecutive_dates, outcome_rows

UTC = dt.timezone.utc
FIXED_NOW = dt.datetime(2026, 3, 16, 4, 15, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parent.parent

_FILE_SCHEMA = """
CREATE TABLE trip_outcomes (
  trip_id TEXT, service_date TEXT, route_id TEXT, start_utc TEXT, outcome TEXT,
  PRIMARY KEY (trip_id, service_date));
CREATE TABLE heartbeats (ts_utc TEXT PRIMARY KEY, ok INTEGER);
"""


def make_file_db(path: Path, days, extra_rows=()):
    db = sqlite3.connect(path)
    db.executescript(_FILE_SCHEMA)
    for day in days:
        db.executemany("INSERT INTO trip_outcomes VALUES (?,?,?,?,?)",
                       outcome_rows(day))
    db.executemany("INSERT INTO trip_outcomes VALUES (?,?,?,?,?)", extra_rows)
    db.execute("INSERT INTO heartbeats VALUES ('2026-03-02T00:00:00.100000+00:00',1)")
    db.commit()
    db.close()
    return path


def tree(root: Path):
    return {str(p.relative_to(root)).replace("\\", "/"): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_gate_failure_writes_nothing_and_raises(tmp_path):
    db = build_db()
    db.execute("INSERT INTO trip_outcomes VALUES "
               "('bad','2026-03-23','R1','2026-03-23T20:00:00+00:00','MAYBE')")
    db.commit()
    data_dir = tmp_path / "data"
    with pytest.raises(GateFailed):
        write_dataset(db, data_dir, today=dt.date(2026, 3, 24), now_utc=FIXED_NOW)
    assert not data_dir.exists()


def test_gate_failure_leaves_the_previous_publish_untouched(tmp_path):
    # Stale but verified data stays up; it is never replaced by numbers that
    # failed their own checks.
    data_dir = tmp_path / "data"
    write_dataset(build_db(service_dates=consecutive_dates(14)), data_dir,
                  today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    before = tree(data_dir)
    bad = build_db(service_dates=consecutive_dates(14))
    bad.execute("INSERT INTO trip_outcomes VALUES "
                "('bad','2026-03-02','R1','2026-03-02T20:00:00+00:00','MAYBE')")
    bad.commit()
    with pytest.raises(GateFailed):
        write_dataset(bad, data_dir, today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    assert tree(data_dir) == before


def test_cli_gate_failure_exits_nonzero_and_leaves_no_files(tmp_path):
    dbfile = make_file_db(
        tmp_path / "bad.db", consecutive_dates(14),
        extra_rows=[("bad", "2026-03-02", "R1",
                     "2026-03-02T20:00:00+00:00", "MAYBE")])
    data_dir = tmp_path / "data"
    proc = subprocess.run(
        [sys.executable, "-m", "publish.dataset", "--db", str(dbfile),
         "--data-dir", str(data_dir)],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr
    assert "wrote nothing" in proc.stderr
    assert not data_dir.exists()


def test_cli_happy_path_writes_the_dataset_and_exits_zero(tmp_path):
    dbfile = make_file_db(tmp_path / "good.db", consecutive_dates(14))
    data_dir = tmp_path / "data"
    proc = subprocess.run(
        [sys.executable, "-m", "publish.dataset", "--db", str(dbfile),
         "--data-dir", str(data_dir)],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (data_dir / "manifest.json").exists()
    assert len(list((data_dir / "daily").iterdir())) == 14


def test_cli_exposes_exactly_the_flags_the_publisher_uses():
    proc = subprocess.run(
        [sys.executable, "-m", "publish.dataset", "--help"],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--db" in proc.stdout
    assert "--data-dir" in proc.stdout
    assert "--commit" not in proc.stdout


# Modules that are themselves a way to spawn a process or speak git, so
# importing them at all - anywhere in the file, at any nesting depth - is
# disqualifying regardless of which names are pulled out of them.
_FORBIDDEN_MODULE_ROOTS = {"subprocess", "pty", "commands", "git", "pygit2", "dulwich"}
# os.* and shutil.* are legitimate (dataset.py already uses os-adjacent paths
# and shutil.rmtree), so only the specific process-spawning members are
# forbidden, not the modules themselves.
_FORBIDDEN_OS_NAMES = {"system", "popen"}
_FORBIDDEN_OS_PREFIXES = ("exec", "spawn")
_FORBIDDEN_SHUTIL_NAMES = {"which"}


def _is_forbidden_os_attr(name: str) -> bool:
    return name in _FORBIDDEN_OS_NAMES or name.startswith(_FORBIDDEN_OS_PREFIXES)


def _process_spawn_violations(source: str) -> list[str]:
    """Every import or attribute access in `source` that could spawn a process
    or invoke git, found by walking the real AST rather than grepping text.

    ast.walk visits every node in the tree regardless of nesting - a
    function-local `import subprocess` inside main() is exactly as visible to
    it as a module-level one, which a substring/line-based scan could miss
    depending on how it was written.
    """
    violations = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _FORBIDDEN_MODULE_ROOTS:
                    violations.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            if root in _FORBIDDEN_MODULE_ROOTS:
                violations.append(f"from {module} import ...")
            elif root == "os":
                violations += [f"from os import {a.name}" for a in node.names
                              if _is_forbidden_os_attr(a.name)]
            elif root == "shutil":
                violations += [f"from shutil import {a.name}" for a in node.names
                              if a.name in _FORBIDDEN_SHUTIL_NAMES]
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            base, attr = node.value.id, node.attr
            if base == "os" and _is_forbidden_os_attr(attr):
                violations.append(f"os.{attr}")
            if base == "shutil" and attr in _FORBIDDEN_SHUTIL_NAMES:
                violations.append(f"shutil.{attr}")
    return violations


def test_the_dataset_module_never_touches_git():
    """publish/dataset.py must be structurally incapable of invoking git, not
    merely silent about it in its own prose.

    The brief's original version of this test asserted `"push" not in
    source` - a vocabulary check, not a capability check. It false-positived
    on two sentences that are real, load-bearing documentation of the
    credential split (spec D4: the VM's publish step must not be able to push
    arbitrary HTML, so this module only ever writes files and a separate
    script, ops/publish.sh, owns git): the module's own top-of-file docstring
    ("committing and pushing is ops/publish.sh's job") and this task's own
    verbatim main() docstring ("ops/publish.sh commits and pushes"). Deleting
    the word "push" from that prose to satisfy the check would have removed
    real explanation to appease a crude test, so the test was replaced
    instead: it now asserts the actual property - that the module cannot
    spawn a process or import a git library - by parsing its AST instead of
    matching its spelling.
    """
    assert not hasattr(dataset, "_git_publish")
    source = inspect.getsource(dataset)
    violations = _process_spawn_violations(source)
    assert not violations, f"process-spawning capability found: {violations}"


def test_two_runs_are_byte_identical(tmp_path):
    days = consecutive_dates(14)
    first, second = tmp_path / "first", tmp_path / "second"
    write_dataset(build_db(service_dates=days), first,
                  today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    write_dataset(build_db(service_dates=days), second,
                  today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    assert tree(first) == tree(second)


def test_rerunning_into_the_same_directory_is_idempotent(tmp_path):
    days = consecutive_dates(14)
    data_dir = tmp_path / "data"
    db = build_db(service_dates=days)
    write_dataset(db, data_dir, today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    before = tree(data_dir)
    write_dataset(db, data_dir, today=dt.date(2026, 3, 16), now_utc=FIXED_NOW)
    assert tree(data_dir) == before
