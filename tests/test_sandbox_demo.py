"""The demo month against hand-built oracles, in the real sandbox. No API calls;
skipped when Docker or the linus-sandbox image is unavailable."""

import pytest

from linus import demo, diffcheck, sandbox
from linus.oracle import TEST_NAME, Oracle, base_run, render, reproduces
from linus.source import LocalSource
from linus.verify import verify

try:
    sandbox.image_id()
    DOCKER = True
except sandbox.InfrastructureError:
    DOCKER = False
pytestmark = pytest.mark.skipif(not DOCKER, reason="needs Docker and the linus-sandbox image")

IMPORTS = ["from textkit import chunk, parse_duration, slugify, truncate, word_count"]
CASES = {
    1: [("chunk([1, 2, 3, 4, 5], 2)", [[1, 2], [3, 4], [5]]), ("chunk([1, 2, 3], 5)", [[1, 2, 3]]),
        ("chunk([1, 2, 3, 4], 2)", [[1, 2], [3, 4]])],
    2: [('slugify("Hello, World!")', "hello-world"), ('slugify("  --Rust & Go--  ")', "rust-go"),
        ('slugify("a  b")', "a-b")],
    3: [('parse_duration("1m30s")', 90), ('parse_duration("45s")', 45), ('parse_duration("1h")', 3600)],
    4: [('word_count("hello  world")', 2), ('word_count("")', 0), ('word_count("one\\ttwo\\nthree")', 3),
        ('word_count("   \\t\\n")', 0), ('word_count("  hello world  ")', 2)],
    5: [('truncate("abcdefghij", 5)', "ab..."), ('truncate("short", 10)', "short"),
        ('truncate("abc", 3)', "abc"), ('truncate("abcdefgh", 7)', "abcd...")],
}
ISSUE_OF = {101: 1, 103: 3, 106: 2, 109: 4, 112: 5, 113: 4}


def oracle_for(issue: int) -> Oracle:
    cases = [{"call": call, "expected": repr(value), "raises": None, "from_issue": True}
             for call, value in CASES[issue]]
    return Oracle(issue=issue, valid=True, test_code=render(IMPORTS, cases), test_path=TEST_NAME, cases=cases)


@pytest.fixture(scope="module")
def month(tmp_path_factory):
    root = demo.build(tmp_path_factory.mktemp("demo") / "month")
    work = tmp_path_factory.mktemp("work")
    source = LocalSource(root)
    return source, {pr.number: pr for pr in source.pulls()}, work


def _verify(month, number: int):
    source, pulls, work = month
    pr = pulls[number]
    signals = diffcheck.analyze(source.diff(pr), source.changes(pr), f"{pr.title}\n{pr.body}")
    oracle = oracle_for(ISSUE_OF[number]) if number in ISSUE_OF else None
    return verify(source, pr, oracle, signals, work)


@pytest.mark.parametrize("issue", sorted(CASES))
def test_hand_oracles_fail_on_base_for_real_reasons(month, issue):
    source, pulls, work = month
    base = next(iter(pulls.values())).base_sha
    result = base_run(source, base, oracle_for(issue).test_code, work, f"base_{issue}")
    assert result.outcome == "fail", result.output_tail
    assert all(reproduces(c) for c in result.cases if c["outcome"] == "failure")


@pytest.mark.parametrize("number", [101, 103, 106, 108, 109])
def test_honest_prs_are_verified(month, number):
    result = _verify(month, number)
    assert result.outcome == "verified", (result.reason, result.runs)


def test_conftest_that_forces_exit_status_does_not_load_for_the_oracle(month):
    result = _verify(month, 112)
    assert result.outcome == "not_fixed", (result.reason, result.runs)


def test_always_equal_int_subclass_fails_the_strict_oracle(month):
    result = _verify(month, 113)
    assert result.outcome == "not_fixed", (result.reason, result.runs)
    assert any("not a plain JSON value" in f for f in result.runs[0]["failures"])


def test_forced_exit_status_is_caught_in_the_suite_run_too(tmp_path):
    """A conftest that zeroes the exit status still loads for the PR's own suite;
    the failure count from the JUnit report keeps the run from passing."""
    tree = tmp_path / "tree"
    (tree / "tests").mkdir(parents=True)
    (tree / "tests" / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 0\n")
    (tree / "tests" / "test_x.py").write_text("def test_ok():\n    pass\n\ndef test_bad():\n    assert 0\n")
    result = sandbox.pytest(tree, ["tests"], label="forced_exit")
    assert result.exit_code == 0 and result.failed == 1
    assert result.outcome == "error"


def test_tampered_report_is_flagged_as_possible_forgery(tmp_path):
    """Code that rewrites the printed summary disagrees with the JUnit report."""
    tree = tmp_path / "tree"
    (tree / "tests").mkdir(parents=True)
    (tree / "tests" / "conftest.py").write_text("import atexit\n\natexit.register(print, '3 passed in 0.01s')\n")
    (tree / "tests" / "test_x.py").write_text("def test_ok():\n    pass\n")
    result = sandbox.pytest(tree, ["tests"], label="fake_summary")
    assert result.outcome == "error" and "forgery" in result.problem


def test_dangling_symlink_does_not_crash(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "dangling").symlink_to(tmp_path / "missing")
    (tree / "test_x.py").write_text("def test_ok():\n    pass\n")
    assert sandbox.pytest(tree, ["test_x.py"], label="symlink").outcome == "pass"
