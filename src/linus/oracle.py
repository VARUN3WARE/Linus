"""The oracle: one reproduction test per issue, judged against the issue only.
It never sees a PR, so it can't be shaped by one, and every PR for the same
issue is judged by the same test.

Three steps, each owned by the right tool:
1. OpenAI proposes test cases as structured JSON (call + expected value).
2. JEV audits every case against the issue's stated rules, in one request,
   and cases it finds inconsistent are dropped. (v2 lost a correct PR to a
   generated case that contradicted the issue; this is the fix.)
3. Code renders the surviving cases into a pytest file, and the sandbox
   proves it fails on the buggy base code. If it doesn't, the output goes
   back to the model for another try (bounded).
"""

import ast
import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from typesafe_sdk import Noul

from linus.jev import Jev
from linus.llm import Generator
from linus.sandbox import RunResult, pytest
from linus.source import Issue, LocalSource
from linus.tracing import annotate, step

MAX_ATTEMPTS = 3
MIN_CASES = 2
KEEP_THRESHOLD = 0.5  # JEV probability that a case is consistent with the issue
SOURCE_BUDGET = 40_000  # characters of base source shown to the model
TEST_NAME = "test_linus_repro.py"
# Failing on the base code only counts if the test fails for a real reason. These
# mean the test itself is broken (v3 "reproduced" a bug with NameError).
AUTHORING_ERRORS = ("NameError", "ImportError", "ModuleNotFoundError", "SyntaxError",
                    "IndentationError", "fixture")

SYSTEM = """You write reproduction test cases for bug reports in Python projects.

Reply with JSON only, in this shape:
{"imports": ["<import line>", ...],
 "cases": [{"call": "<python expression>", "expected": <JSON value>,
            "raises": null, "from_issue": true}, ...]}

Rules:
- imports: import the package exactly the way the existing tests do.
- call: one expression that calls the public API, e.g. slugify("a b").
- expected: the value the call must return, as a plain JSON value (string, number,
  boolean, null, array or object). If the issue says the call must raise, set "raises"
  to the exception name and "expected" to null.
- Include every example the issue gives (from_issue: true), plus at least three more
  cases that follow the same rule with different inputs (from_issue: false), so that a
  fix hard-coded to the issue's examples would fail.
- Only test behavior the issue states. Skip inputs whose correct output the issue
  does not settle.
- Work out each expected value carefully against the issue's rule before writing it.
- Deterministic only: no network, files, clock or randomness."""


@dataclass
class Oracle:
    issue: int
    valid: bool
    test_code: str = ""
    test_sha256: str = ""
    test_path: str = ""
    cases: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    model: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict) -> "Oracle":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _parse_cases(text: str) -> tuple[list[str], list[dict]]:
    match = re.search(r"\{.*\}", text, re.S)
    data = json.loads(match.group(0) if match else text)
    imports = [line for line in data.get("imports", []) if re.match(r"^(from|import)\s", line)]
    cases = []
    for case in data.get("cases", []):
        try:
            ast.parse(case["call"], mode="eval")
            if case.get("raises") and not re.fullmatch(r"[A-Za-z_][\w.]*", case["raises"]):
                continue
        except (SyntaxError, KeyError, TypeError):
            continue
        # Expected values arrive as JSON and are rendered with repr(), so a string
        # like "hello-world" can never be read as Python code (v3 bug).
        expected = None if case.get("raises") else repr(case.get("expected"))
        cases.append({"call": case["call"], "expected": expected,
                      "raises": case.get("raises"), "from_issue": bool(case.get("from_issue"))})
    return imports, cases


def render(imports: list[str], cases: list[dict]) -> str:
    lines = ["import pytest", "", *imports, ""]
    for i, case in enumerate(cases):
        lines += ["", f"def test_repro_{i}():"]
        if case["raises"]:
            lines += [f"    with pytest.raises({case['raises']}):", f"        {case['call']}"]
        else:
            lines.append(f"    assert {case['call']} == {case['expected']}")
    return "\n".join(lines) + "\n"


def audit(jev: Jev, issue: Issue, cases: list[dict]) -> list[float]:
    """JEV's probability that each case is consistent with the issue's rules."""
    shown = [{"call": c["call"], "expected": f"raises {c['raises']}" if c["raises"] else c["expected"]}
             for c in cases]
    questions = {
        f"case_{i}": Noul(
            instructions=f"Is `cases[{i}].expected` consistent with every rule and example stated in "
            f"`issue` for the input in `cases[{i}].call`?",
            criteria={"true": "The expected value follows the issue's rules and examples",
                      "false": "The expected value contradicts a rule or example the issue states"},
        )
        for i in range(len(cases))
    }
    response = jev.ask(f"oracle_audit.issue_{issue.number}",
                       {"issue": {"title": issue.title, "body": issue.body}, "cases": shown}, questions)
    return [response.nouls[f"case_{i}"].noul for i in range(len(cases))]


def _test_dir(files: list[str]) -> str:
    for candidate in ("tests", "test"):
        if any(f.startswith(candidate + "/") for f in files):
            return candidate
    return "."


def _context(source: LocalSource, base_sha: str, focus: set[str]) -> tuple[str, str]:
    """Base source code and existing tests to show the model."""
    files = source.files_at(base_sha)
    code_files = [f for f in files if f.endswith(".py") and not re.search(r"(^|/)tests?/|test_", f)]
    total = sum(len(source.read(base_sha, f) or "") for f in code_files)
    if total > SOURCE_BUDGET:
        code_files = [f for f in code_files if f in focus] or code_files[:5]
    code = "\n\n".join(f"# file: {f}\n{source.read(base_sha, f)}" for f in code_files)[:SOURCE_BUDGET]
    test_files = [f for f in files if f.endswith(".py") and re.search(r"(^|/)tests?/|test_", f)]
    tests = "\n\n".join(f"# file: {f}\n{source.read(base_sha, f)}" for f in test_files[:3])[:8_000]
    config = "\n\n".join(f"# file: {f}\n{source.read(base_sha, f)}"
                         for f in ("pyproject.toml", "setup.cfg", "pytest.ini") if f in files)
    return code, (config + "\n\n" + tests).strip()


def build(gen: Generator, jev: Jev, source: LocalSource, issue: Issue, base_sha: str,
          focus: set[str], workdir: Path) -> Oracle:
    code_ctx, tests_ctx = _context(source, base_sha, focus)
    test_dir = _test_dir(source.files_at(base_sha))
    rel = f"{test_dir}/{TEST_NAME}" if test_dir != "." else TEST_NAME
    oracle = Oracle(issue=issue.number, valid=False, test_path=rel, model=gen.model)
    feedback = ""
    with step(f"oracle.issue_{issue.number}", kind="CHAIN", issue=issue.number) as span:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            prompt = (f"Issue #{issue.number}: {issue.title}\n\n{issue.body}\n\n"
                      f"Current (buggy) source code:\n\n{code_ctx}\n\n"
                      f"Project config and existing tests, for import style:\n\n{tests_ctx}\n")
            if feedback:
                prompt += f"\nYour previous attempt was rejected:\n{feedback}\nReturn corrected JSON.\n"
            generation = gen.complete(SYSTEM, prompt)
            record: dict = {"attempt": attempt, "tokens": generation.prompt_tokens + generation.completion_tokens}
            oracle.attempts.append(record)
            try:
                imports, cases = _parse_cases(generation.text)
            except (json.JSONDecodeError, AttributeError) as exc:
                feedback = f"The reply was not valid JSON ({exc})."
                record["problem"] = "invalid json"
                continue

            scores = audit(jev, issue, cases) if cases else []
            kept = [c | {"jev": round(p, 3)} for c, p in zip(cases, scores) if p >= KEEP_THRESHOLD]
            dropped = [c | {"jev": round(p, 3)} for c, p in zip(cases, scores) if p < KEEP_THRESHOLD]
            record.update(proposed=len(cases), kept=len(kept), dropped=dropped)
            if len(kept) < MIN_CASES:
                feedback = (f"Only {len(kept)} of your cases are consistent with the issue. These contradict "
                            f"it: {json.dumps([{k: c[k] for k in ('call', 'expected')} for c in dropped])}")
                continue

            code = render(imports, kept)
            tree = Path(tempfile.mkdtemp(prefix="base-", dir=workdir))
            try:
                source.export(base_sha, tree)
                (tree / rel).parent.mkdir(parents=True, exist_ok=True)
                (tree / rel).write_text(code)
                result: RunResult = pytest(tree, [rel], label=f"oracle_{issue.number}_try{attempt}")
            finally:
                shutil.rmtree(tree, ignore_errors=True)
            record["base_run"] = result.to_dict()

            broken = [f for f in result.failures if f.split(":")[0].strip() in AUTHORING_ERRORS
                      or "fixture" in f]
            if broken:
                record["problem"] = "test is broken: " + broken[0][:120]
                feedback = ("The test file itself is broken, it fails on its own errors rather than on "
                            f"the bug: {broken[0]}\n\npytest output:\n{result.output_tail}")
                continue
            if result.outcome == "fail" and result.failed >= 1:
                oracle.valid = True
                oracle.test_code = code
                oracle.test_sha256 = hashlib.sha256(code.encode()).hexdigest()
                oracle.cases, oracle.dropped = kept, dropped
                break
            if result.outcome == "pass":
                feedback = ("Every case passed on the current buggy code, so they do not reproduce the "
                            f"bug.\n\npytest output:\n{result.output_tail}")
            else:
                feedback = f"The rendered test file did not run cleanly.\n\npytest output:\n{result.output_tail}"
        annotate(span, valid=oracle.valid, attempts=len(oracle.attempts),
                 cases=len(oracle.cases), dropped=oracle.dropped)
    return oracle
