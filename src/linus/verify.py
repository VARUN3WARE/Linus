"""Deep verification in the sandbox. For a code PR:

1. The oracle's calls run against the PR's code, one fresh interpreter each,
   and the host compares the results with expected values the sandbox never
   sees. The oracle already failed on the base code, so every case passing
   here is the fail-to-pass proof.
2. The base branch's tests run against the PR's code from a second tree: the
   PR's code with every test file and pytest config replaced by the base
   branch's, at their original paths (fixtures included). It runs in its own
   pytest process, so nothing the PR adds to its tests (a tests/__init__.py, a
   conftest) can shadow or skip them, and every base test must show up in the
   JUnit report. This catches PRs that delete or skip the test they break.
3. The PR's own tests run as the PR has them.

Every run imports the package from the base tree's import root, so a PR that
adds a second copy of the package can't choose which copy is tested.
Docs-only PRs skip step 1.
"""

import ast
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from linus import oracle as oracle_mod
from linus import sandbox
from linus.diffcheck import TEST_PATH, DiffSignals
from linus.oracle import Oracle
from linus.source import LocalSource, PullRequest

# Root-level files that configure pytest: the base run uses the base branch's.
PYTEST_CONFIGS = ("pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")
TEST_MODULE = ("test_", "_test.py")


@dataclass
class Verification:
    outcome: str  # verified | not_fixed | regression | tampering | needs_human | inconclusive
    reason: str
    runs: list[dict] = field(default_factory=list)


def _is_base_test_file(path: str) -> bool:
    return bool(TEST_PATH.search(path)) or path in PYTEST_CONFIGS


def _files(tree: Path) -> list[str]:
    return sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file() or p.is_symlink())


def base_tests_tree(pr_tree: Path, base_tree: Path, dest: Path) -> list[str]:
    """Build the tree the base tests run from: the PR's files minus its tests and
    pytest config, plus the base branch's, copied byte for byte. Returns the
    base test files placed."""
    shutil.copytree(pr_tree, dest, symlinks=True)
    for path in _files(dest):
        if _is_base_test_file(path):
            (dest / path).unlink()
    placed = []
    for path in _files(base_tree):
        if _is_base_test_file(path) and not (base_tree / path).is_symlink():
            (dest / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(base_tree / path, dest / path)
            placed.append(path)
    return placed


def _test_names(path: str, source: str) -> set[tuple[str, str]]:
    """(JUnit classname, test name) for the tests pytest collects from a base
    test module: module-level test functions and test methods of Test classes."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    module = path[:-3].replace("/", ".")
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
    names = {(module, node.name) for node in tree.body if isinstance(node, funcs) and node.name.startswith("test")}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            names |= {(f"{module}.{node.name}", item.name) for item in node.body
                      if isinstance(item, funcs) and item.name.startswith("test")}
    return names


def expected_tests(base_tree: Path, placed: list[str]) -> set[tuple[str, str]]:
    expected = set()
    for path in placed:
        name = path.rsplit("/", 1)[-1]
        if path.endswith(".py") and (name.startswith(TEST_MODULE[0]) or name.endswith(TEST_MODULE[1])):
            expected |= _test_names(path, (base_tree / path).read_text(errors="replace"))
    return expected


def missing_tests(expected: set[tuple[str, str]], result: sandbox.RunResult) -> list[str]:
    """Base tests the JUnit report doesn't mention. Parametrized ids count for their function."""
    seen = {(c.get("classname", ""), c["name"].split("[", 1)[0]) for c in result.cases}
    return sorted(f"{cls}::{name}" for cls, name in expected - seen)


def _packages(files: list[str], root: str) -> set[str]:
    prefix = f"{root}/" if root else ""
    names = set()
    for path in files:
        if path.startswith(prefix) and path.endswith(".py"):
            parts = path[len(prefix):].split("/")
            if not (root == "" and parts[0] == "src"):
                names.add(parts[0][:-3] if len(parts) == 1 else parts[0])
    return names


def layout_problems(base_files: list[str], pr_files: list[str]) -> list[str]:
    """New import roots or second copies of a package the PR adds. Tests always
    import from the base's root, so these can't change a verdict, but a person
    should see why a PR ships a parallel copy of the code."""
    base_root = "src" if sandbox.import_roots(base_files) != [sandbox.SRC_MOUNT] else ""
    other = "" if base_root else "src"
    added = sorted(set(pr_files) - set(base_files))
    problems = []
    if not base_root and any(f.startswith("src/") and f.endswith(".py") for f in added):
        problems.append("adds src/, a new import root (the base branch has a flat layout)")
    copies = (_packages(added, other) & _packages(base_files, base_root)) - _packages(base_files, other)
    problems += [f"adds a second copy of package {name!r} outside {base_root or 'the repo root'}/"
                 for name in sorted(copies)]
    return problems


def _run_oracle(tree: Path, pr: PullRequest, oracle: Oracle, roots: list[str],
                runs: list[dict]) -> Verification | None:
    repro = oracle_mod.run(tree, oracle.imports, oracle.cases, f"pr{pr.number}_oracle", roots)
    runs.append(repro.to_dict())
    if repro.outcome == "error":
        return Verification("inconclusive", repro.problem or "oracle calls did not run cleanly on the PR code",
                            runs)
    if repro.outcome == "fail":
        total = repro.passed + repro.failed
        return Verification(
            "not_fixed", f"reproduction test still fails on the PR ({repro.failed} of {total} failing)", runs)
    if repro.passed != oracle.case_count:
        return Verification("inconclusive", f"oracle has {oracle.case_count} cases but {repro.passed} passed",
                            runs)
    return None


def _failing(result: sandbox.RunResult, what: str, signals: DiffSignals, runs: list[dict]) -> Verification:
    if result.outcome == "error":
        return Verification("inconclusive", result.problem or f"{what} did not run cleanly on the PR code", runs)
    if signals.tampering:
        removed = ", ".join(signals.removed_tests) or f"{signals.added_skips} skip marker(s)"
        return Verification("tampering", f"deletes or skips tests ({removed}) that its change breaks", runs)
    if signals.touches_tests:
        return Verification("needs_human", f"{what} fail and the PR also edits tests", runs)
    return Verification("regression", f"breaks {what} ({result.failed} failing)", runs)


def _targets(placed: list[str]) -> list[str]:
    """Top-level test directories and root-level test modules to hand pytest
    (not a root conftest.py: pytest loads that by itself)."""
    targets = set()
    for path in placed:
        if "/" in path:
            targets.add(path.split("/", 1)[0])
        elif path.endswith(".py") and (path.startswith(TEST_MODULE[0]) or path.endswith(TEST_MODULE[1])):
            targets.add(path)
    return sorted(targets)


def verify(source: LocalSource, pr: PullRequest, oracle: Oracle | None,
           signals: DiffSignals, workdir: Path) -> Verification:
    scratch = Path(tempfile.mkdtemp(prefix=f"pr{pr.number}-", dir=workdir))
    tree, base_tree, tests_tree = scratch / "pr", scratch / "base", scratch / "base_tests"
    runs: list[dict] = []
    try:
        source.export(pr.head_sha, tree)
        base_files = source.files_at(pr.base_sha)
        roots = sandbox.import_roots(base_files)

        if oracle is not None and (early := _run_oracle(tree, pr, oracle, roots, runs)):
            return early

        source.export(pr.base_sha, base_tree)
        placed = base_tests_tree(tree, base_tree, tests_tree)
        base = None
        if targets := _targets(placed):
            base = sandbox.pytest(tests_tree, targets, label=f"pr{pr.number}_base_tests", roots=roots)
            runs.append(base.to_dict())
        own = None
        if own_targets := [d for d in ("tests", "test") if (tree / d).is_dir()]:
            own = sandbox.pytest(tree, own_targets, label=f"pr{pr.number}_suite", roots=roots)
            runs.append(own.to_dict())

        if base is None and own is None:
            return Verification("inconclusive", "no tests to run on the PR code", runs)
        if base is not None and base.outcome != "pass":
            return _failing(base, "the base branch's tests", signals, runs)
        if base is not None and (missing := missing_tests(expected_tests(base_tree, placed), base)):
            return Verification("inconclusive", f"base tests missing from the JUnit report: "
                                f"{', '.join(missing[:5])}", runs)
        if own is not None and own.outcome != "pass":
            return _failing(own, "the PR's own tests", signals, runs)

        proof = "fails on base, passes on PR" if oracle else "docs only"
        parts = [f"base tests green ({base.passed} passed)" if base is not None else "",
                 f"the PR's own tests green ({own.passed} passed)" if own is not None else ""]
        reason = f"{proof}; " + " and ".join(p for p in parts if p)
        if problems := layout_problems(base_files, _files(tree)):
            return Verification("needs_human", f"{reason}, but the PR {'; '.join(problems)}", runs)
        return Verification("verified", reason, runs)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
