"""Deep verification in the sandbox. For a code PR:

1. The oracle test runs against the PR's code, from a Linus-owned directory
   with Linus's own pytest config (the PR's conftest and settings never load).
   It already failed on the base code, so every case passing here is the
   fail-to-pass proof.
2. The PR's own tests plus a copy of the base branch's tests run against the
   PR's code. Running the base tests catches PRs that delete or skip the very
   test their change breaks.

Docs-only PRs skip step 1.
"""

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from linus.diffcheck import TEST_PATH, DiffSignals
from linus.oracle import Oracle
from linus.sandbox import pytest, run_oracle
from linus.source import LocalSource, PullRequest

BASE_TESTS_DIR = "linus_base_tests"


@dataclass
class Verification:
    outcome: str  # verified | not_fixed | regression | tampering | needs_human | inconclusive
    reason: str
    runs: list[dict] = field(default_factory=list)


def _copy_base_tests(source: LocalSource, base_sha: str, tree: Path) -> int:
    count = 0
    for path in source.files_at(base_sha):
        if TEST_PATH.search(path) and path.endswith(".py"):
            target = tree / BASE_TESTS_DIR / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read(base_sha, path) or "")
            count += 1
    return count


def verify(source: LocalSource, pr: PullRequest, oracle: Oracle | None,
           signals: DiffSignals, workdir: Path) -> Verification:
    tree = Path(tempfile.mkdtemp(prefix=f"pr{pr.number}-", dir=workdir))
    runs: list[dict] = []
    try:
        source.export(pr.head_sha, tree)

        if oracle is not None:
            repro = run_oracle(tree, oracle.test_code, label=f"pr{pr.number}_oracle")
            runs.append(repro.to_dict())
            if repro.outcome == "error":
                why = repro.problem or "oracle test did not run cleanly on the PR code"
                return Verification("inconclusive", why, runs)
            if repro.outcome == "fail":
                total = repro.passed + repro.failed
                return Verification(
                    "not_fixed", f"reproduction test still fails on the PR ({repro.failed} of {total} failing)",
                    runs)
            if repro.passed != oracle.case_count:
                return Verification(
                    "inconclusive", f"oracle has {oracle.case_count} cases but {repro.passed} passed", runs)

        copied = _copy_base_tests(source, pr.base_sha, tree)
        targets = [d for d in ("tests", "test") if (tree / d).is_dir()]
        if copied:
            targets.append(BASE_TESTS_DIR)
        suite = pytest(tree, targets or ["."], label=f"pr{pr.number}_suite")
        runs.append(suite.to_dict())

        if suite.outcome == "pass":
            proof = "fails on base, passes on PR" if oracle else "docs only"
            return Verification("verified", f"{proof}; full suite incl. base tests green ({suite.passed} passed)", runs)
        if suite.outcome == "error":
            why = suite.problem or "test suite did not run cleanly on the PR code"
            return Verification("inconclusive", why, runs)
        if signals.tampering:
            removed = ", ".join(signals.removed_tests) or f"{signals.added_skips} skip marker(s)"
            return Verification(
                "tampering", f"deletes or skips tests ({removed}) that its change breaks", runs)
        if signals.touches_tests:
            return Verification("needs_human", "existing tests fail and the PR also edits tests", runs)
        return Verification("regression", f"breaks existing tests ({suite.failed} failing)", runs)
    finally:
        shutil.rmtree(tree, ignore_errors=True)
