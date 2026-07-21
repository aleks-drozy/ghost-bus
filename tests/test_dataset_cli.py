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


def test_cli_gate_failure_leaves_a_previous_publish_untouched(tmp_path):
    """The case the systemd timer actually hits, not the empty-directory
    edge case: every run after the first starts with a previous publish
    already on disk. test_cli_gate_failure_exits_nonzero_and_leaves_no_files
    only proves nothing is created from nothing; this proves a real,
    already-published dataset survives byte-for-byte when a later run's
    gate fails, driven through the actual CLI end to end rather than calling
    write_dataset directly."""
    days = consecutive_dates(14)
    data_dir = tmp_path / "data"

    good_db = make_file_db(tmp_path / "good.db", days)
    first = subprocess.run(
        [sys.executable, "-m", "publish.dataset", "--db", str(good_db),
         "--data-dir", str(data_dir)],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert first.returncode == 0, first.stdout + first.stderr
    before = tree(data_dir)
    assert before  # sanity: the first run actually published something

    bad_db = make_file_db(
        tmp_path / "bad.db", days,
        extra_rows=[("bad", "2026-03-02", "R1",
                     "2026-03-02T20:00:00+00:00", "MAYBE")])
    second = subprocess.run(
        [sys.executable, "-m", "publish.dataset", "--db", str(bad_db),
         "--data-dir", str(data_dir)],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert second.returncode == 1, second.stdout + second.stderr
    assert "wrote nothing" in second.stderr
    assert tree(data_dir) == before


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
_FORBIDDEN_OS_NAMES = {"system", "popen", "posix_spawn", "posix_spawnp", "startfile"}
_FORBIDDEN_OS_PREFIXES = ("exec", "spawn")
_FORBIDDEN_SHUTIL_NAMES = {"which"}
# The two ways stdlib code launches another interpreter/module by name
# instead of by `import` statement - both take the module name as data, so a
# forbidden target reached through either is otherwise invisible to the
# import-node checks above.
_DYNAMIC_IMPORT_TARGETS = {("builtins", "__import__"), ("importlib", "import_module")}


def _is_forbidden_os_attr(name: str) -> bool:
    return name in _FORBIDDEN_OS_NAMES or name.startswith(_FORBIDDEN_OS_PREFIXES)


def _is_forbidden_target(module_root: str, attr: str) -> bool:
    if module_root == "os":
        return _is_forbidden_os_attr(attr)
    if module_root == "shutil":
        return attr in _FORBIDDEN_SHUTIL_NAMES
    return False


def _module_alias_map(tree: ast.AST) -> dict[str, str]:
    """local name -> real module root, for every `import X` / `import X as Y`
    anywhere in the tree (any nesting depth, via ast.walk).

    `import os as o` maps "o" -> "os", so a later `o.system(...)` resolves to
    its real target instead of hiding behind the alias. Every bare `import X`
    also maps "X" -> "X", so callers can look a name up unconditionally via
    `.get(name, name)` whether or not it was ever aliased.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                aliases[alias.asname or root] = root
    return aliases


def _callable_alias_map(tree: ast.AST) -> dict[str, tuple[str, str]]:
    """local name -> (real module root, real attribute name), for every
    `from MODULE import NAME [as ALIAS]` anywhere in the tree, restricted to
    os/shutil/importlib origins - the only modules this check cares about.

    `from os import system as s` maps "s" -> ("os", "system"), so a later
    bare call `s()` is recognised as the os module's shell-spawning "system"
    function even though the call site never writes that name itself.
    (Prose only, below - this module never calls it; it only ever detects
    the pattern in the module *under test*.)
    """
    aliases: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in ("os", "shutil", "importlib"):
                for alias in node.names:
                    aliases[alias.asname or alias.name] = (root, alias.name)
    return aliases


def _string_literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _process_spawn_violations(source: str) -> list[str]:
    """Every import, attribute access, or dynamic-import call in `source`
    that could spawn a process or reach a git library - found by walking the
    real AST, resolving aliases, rather than grepping text.

    ast.walk visits every node regardless of nesting, so a function-local
    `import subprocess` inside main() is exactly as visible to it as a
    module-level one. Aliasing (`import os as o`, `from os import system as
    s`) is resolved through _module_alias_map / _callable_alias_map before
    matching, so renaming the import does not defeat the check. A call to
    `__import__` or `importlib.import_module` is treated as a violation
    whenever its argument names a forbidden module OR is not a literal
    string at all - a dynamic import of a computed module name has no
    legitimate reason to exist in this module, so being unable to prove it
    is safe is treated as unsafe.
    """
    violations: list[str] = []
    tree = ast.parse(source)
    module_aliases = _module_alias_map(tree)
    callable_aliases = _callable_alias_map(tree)

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
            elif root in ("os", "shutil"):
                violations += [f"from {module} import {a.name}" for a in node.names
                              if _is_forbidden_target(root, a.name)]

        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            base = module_aliases.get(node.value.id, node.value.id)
            if _is_forbidden_target(base, node.attr):
                violations.append(f"{node.value.id}.{node.attr} (resolves to {base}.{node.attr})")

        elif isinstance(node, ast.Call):
            func = node.func
            target = None
            if isinstance(func, ast.Name):
                if func.id == "__import__":
                    target = ("builtins", "__import__")
                elif func.id in callable_aliases:
                    target = callable_aliases[func.id]
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                target = (module_aliases.get(func.value.id, func.value.id), func.attr)

            if target in _DYNAMIC_IMPORT_TARGETS:
                arg = node.args[0] if node.args else None
                literal = _string_literal(arg)
                if arg is None:
                    violations.append(f"{target[1]}() called with no positional argument")
                elif literal is None:
                    violations.append(
                        f"{target[1]}() called with a non-literal module name - "
                        "a dynamic import of a computed name cannot be shown safe")
                elif literal.split(".")[0] in _FORBIDDEN_MODULE_ROOTS:
                    violations.append(f"{target[1]}({literal!r})")
            elif target is not None and _is_forbidden_target(*target):
                violations.append(f"call to {target[0]}.{target[1]}")

    return violations


def test_the_dataset_module_never_touches_git():
    """Regression guard, not a proof: catches straightforward reintroduction
    of process-spawning or git-library access in publish/dataset.py,
    including common aliased and dynamically-imported forms.

    No static check can show a module is *incapable* of invoking git - a
    sufficiently indirect construction (reflection built from concatenated
    strings, ctypes, ships-its-own-bytecode, a monkeypatch at import time,
    ...) always escapes source-level analysis, and this test does not claim
    otherwise. The real assurance is architectural: the VM's publish
    credential cannot push to the site repo at all (spec D4), so this
    module only ever writes files while a separate script, ops/publish.sh,
    holds the git credential. What this test does is watch this module's
    side of that split, so an accidental `import subprocess` - or an
    aliased or dynamically-constructed equivalent - added here in a future
    change is caught by the suite instead of shipped to the VM.

    The brief's original version of this test asserted `"push" not in
    source` - a vocabulary check, not a capability check. It false-positived
    on two sentences that are real, load-bearing documentation of the same
    credential split: this module's own top-of-file docstring ("committing
    and pushing is ops/publish.sh's job") and this task's own verbatim
    main() docstring ("ops/publish.sh commits and pushes"). Deleting the
    word "push" from that prose to satisfy the check would have removed
    real explanation to appease a crude test, so the test was replaced
    instead of the docs; the AST-based version below cares about imports
    and calls, not spelling, so it does not react to either sentence.
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
