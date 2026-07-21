"""Pins the D3/D4 import boundary between the two halves of the publish
split: publish/site.py runs in CI against the PUBLISHED dataset (D3 - it
never opens the database) and must have no code path back into
publish/dataset.py, which holds ghostbus_config.get_db and run_checks.

Mirrors tests/test_dataset_cli.py's test_the_dataset_module_never_touches_git,
the D4 AST test for the other side of the same split - same shape, applied to
the other forbidden edge.
"""
import ast
import inspect

import publish.site as site

# COUNT_FIELDS/RATE_FIELDS in publish/site.py duplicate
# publish.dataset.DAILY_COLUMNS on purpose. `from publish.dataset import
# DAILY_COLUMNS` looks like the obvious DRY refactor to remove that
# duplication, but importing publish.dataset at all - for one constant or
# for anything else - loads that module (and everything it imports:
# ghostbus_config.get_db, run_checks) into this process's reach. The
# boundary this test pins is the import itself, not which names cross it.
_FORBIDDEN_EXACT_MODULES = {"publish.dataset", "ghostbus_config", "run_checks"}
_FORBIDDEN_FROM_PUBLISH_NAMES = {"dataset"}


def _import_violations(source: str) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _FORBIDDEN_EXACT_MODULES:
                    violations.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in _FORBIDDEN_EXACT_MODULES:
                violations.append(f"from {module} import ...")
            elif module == "publish":
                violations += [
                    f"from publish import {alias.name}"
                    for alias in node.names
                    if alias.name in _FORBIDDEN_FROM_PUBLISH_NAMES
                ]
    return violations


def test_site_module_never_imports_the_dataset_module_or_db_access():
    """A regression guard, not a proof - same caveat as the dataset-side
    test this mirrors: a sufficiently indirect construction always escapes
    source-level analysis. The real assurance is architectural (CI's own
    checkout of ghost-bus-data holds no executable code at all, spec D4), so
    what this test does is catch an accidental or well-intentioned-but-wrong
    `import publish.dataset` (or a direct `ghostbus_config`/`run_checks`
    import) in publish/site.py before it ships, the same way the dataset-side
    test catches an accidental `import subprocess` there.
    """
    source = inspect.getsource(site)
    violations = _import_violations(source)
    assert not violations, f"forbidden import found in publish/site.py: {violations}"
