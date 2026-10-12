"""The oracle: one reproduction test per issue, judged against the issue only.
It never sees a PR, so it can't be shaped by one, and every PR for the same
issue is judged by the same test.

Three steps, each owned by the right tool:
1. OpenAI proposes test cases as structured JSON (call + expected value).
2. JEV audits every case against the issue's stated rules, in one request,
   and cases it finds inconsistent are dropped. (v2 lost a correct PR to a
   generated case that contradicted the issue; this is the fix.)
3. The sandbox runs each surviving call on the buggy base code and the host
   proves it fails there for a real reason. If it doesn't, the output goes
   back to the model for another try (bounded).

Only the imports and call expressions ever enter the sandbox. Each call runs
in a fresh interpreter and the host compares what came back with the expected
value, so code under test can't forge a pass without knowing the answer.
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

from linus import sandbox
from linus.jev import Jev
from linus.llm import Generator
from linus.sandbox import ORACLE_FILE, RunResult
from linus.source import Issue, LocalSource
from linus.tracing import annotate, step

MAX_ATTEMPTS = 3
MIN_CASES = 2
KEEP_THRESHOLD = 0.5  # JEV probability that a case is consistent with the issue
SOURCE_BUDGET = 40_000  # characters of base source shown to the model
TEST_NAME = ORACLE_FILE
# Bumped when the oracle changes meaning; cached oracles in an older format are
# rebuilt and runs judged under an older format can't be resumed. Format 1
# compared with a plain ==, which the result controls; format 2 compared inside
# the sandbox, where the PR's import-time code could patch the comparison;
# format 3 compares on the host, against values that never enter the sandbox.
FORMAT = 3
# A base failure inside the package that names a function the case calls, with
# the wrong arguments, is the case calling the API wrongly (through a wrapper),
# not the bug.
CALLEE_MISCALL = re.compile(r"(?:[\w.]+\.)?(\w+)\(\) (?:got an unexpected keyword argument|missing \d+ "
                            r"required (?:positional|keyword-only) argument|takes (?:from )?\d+ (?:to \d+ )?"
                            r"positional arguments?)")
# The rendered file is for people (receipts, report): Linus never runs it.
HEADER = ('"""Linus oracle, readable copy. Linus does not run this file: each call runs in a fresh\n'
          "interpreter in the sandbox, and the host compares the JSON text of the result with the\n"
          'expected value, which never enters the sandbox."""')

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
    test_code: str = ""  # readable rendering of imports + cases; its sha256 binds the receipt
    test_sha256: str = ""
    test_path: str = ""
    cases: list[dict] = field(default_factory=list)  # call, expected (a JSON value), raises, from_issue
    dropped: list[dict] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    model: str = ""
    format: int = FORMAT
    imports: list[str] = field(default_factory=list)

    @property
    def case_count(self) -> int:
        """A PR passes only if every case ran and passed."""
        return len(self.cases)

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
        # Expected values stay JSON values: the host compares them as JSON text,
        # and they are only rendered (with repr) into the readable copy.
        expected = None if case.get("raises") else case.get("expected")
        cases.append({"call": case["call"], "expected": expected,
                      "raises": case.get("raises"), "from_issue": bool(case.get("from_issue"))})
    return imports, cases


def render(imports: list[str], cases: list[dict]) -> str:
    """The oracle as a pytest file a person can read and run by hand."""
    lines = [HEADER, "", "import pytest", "", *imports, ""]
    for i, case in enumerate(cases):
        lines += ["", f"def test_repro_{i}():"]
        if case["raises"]:
            lines += [f"    with pytest.raises({case['raises']}):", f"        {case['call']}"]
        else:
            lines.append(f"    assert {case['call']} == {case['expected']!r}")
    return "\n".join(lines) + "\n"


def check(case: dict, result: dict) -> tuple[str, str]:
    """The host's verdict on one call: ("passed", "") or (why it failed, message).

    A value passes only if the result is plain JSON all the way down and its
    JSON text equals the expected value's; a raises case only if the exception
    type name matches. A missing or garbled result line fails."""
    if not result["ok"]:
        if result.get("missing"):
            return "missing", result["error"]
        if case["raises"] and result["error"].split(":", 1)[0] == case["raises"].rsplit(".", 1)[-1]:
            return "passed", ""
        return "error", result["error"]
    if case["raises"]:
        return "no_raise", f"DID NOT RAISE {case['raises']}"
    if not result["plain"]:
        return "not_plain", "result is not a plain JSON value"
    got = json.dumps(result["value"], sort_keys=True)
    want = json.dumps(case["expected"], sort_keys=True)
    if got == want:
        return "passed", ""
    return "mismatch", f"{got[:200]} != {want[:200]}"


def judge(cases: list[dict], run: sandbox.CallsRun, label: str) -> RunResult:
    """Compare the container's raw results with the expected values, on the host."""
    rows = []
    for i, (case, result) in enumerate(zip(cases, run.results)):
        kind, message = check(case, result)
        rows.append({"name": f"test_repro_{i}", "call": case["call"],
                     "outcome": "passed" if kind == "passed" else "failure", "kind": kind,
                     "message": message, "where": result.get("where", ""), "both": False})
    bad = [r for r in rows if r["outcome"] == "failure"]
    return RunResult(
        label=label, command=f"linus oracle driver: {len(cases)} calls, one fresh interpreter each",
        exit_code=run.exit_code if run.problem else int(bool(bad)),
        passed=len(rows) - len(bad), failed=len(bad), errors=0, duration_s=run.duration_s,
        timed_out=run.timed_out, output_tail=run.output_tail,
        failed_tests=[r["name"] for r in bad], failures=[r["message"] for r in bad],
        problem=run.problem, cases=rows,
    )


def run(tree: Path, imports: list[str], cases: list[dict], label: str, roots: list[str]) -> RunResult:
    calls = sandbox.run_calls(tree, imports, [c["call"] for c in cases], label, roots)
    return judge(cases, calls, label)


def reproduces(case: dict) -> bool:
    """A base failure that shows the bug, decided by location first: a wrong
    value, DID NOT RAISE, or an exception whose deepest frame is in the code
    under test. An exception from the case expression itself, the stdlib or
    site-packages is an authoring error, and so is one inside the package that
    names a function the case calls with the wrong arguments (a wrapper)."""
    kind = case.get("kind")
    if kind in ("mismatch", "no_raise"):
        return True
    if kind != "error" or not case.get("where", "").startswith(sandbox.SRC_MOUNT + "/"):
        return False
    miscall = CALLEE_MISCALL.search(case.get("message", ""))
    return not (miscall and re.search(rf"\b{re.escape(miscall.group(1))}\s*\(", case.get("call", "")))


def _outcomes(result: RunResult) -> str:
    """Each case's result on the base code, for the model's next attempt."""
    return "\n".join(f"- {c['call']}: {'passed' if c['outcome'] == 'passed' else c['message'][:200]}"
                     for c in result.cases)


def base_run(source: LocalSource, base_sha: str, imports: list[str], cases: list[dict], workdir: Path,
             label: str) -> RunResult:
    tree = Path(tempfile.mkdtemp(prefix="base-", dir=workdir))
    try:
        source.export(base_sha, tree)
        return run(tree, imports, cases, label, sandbox.import_roots(source.files_at(base_sha)))
    finally:
        shutil.rmtree(tree, ignore_errors=True)


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
    sandbox.image_id()  # fail on a broken Docker before spending any generation
    code_ctx, tests_ctx = _context(source, base_sha, focus)
    oracle = Oracle(issue=issue.number, valid=False, test_path=TEST_NAME, model=gen.model)
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

            label = f"oracle_{issue.number}_try{attempt}"
            result = base_run(source, base_sha, imports, kept, workdir, label)
            record["base_run"] = result.to_dict()

            miscalled = [c for c in result.cases if c["outcome"] == "failure" and not reproduces(c)]
            if miscalled and result.outcome == "fail":
                names = {c["name"] for c in miscalled}
                survivors = [c for i, c in enumerate(kept) if f"test_repro_{i}" not in names]
                record["miscalled"] = [c["message"][:200] for c in miscalled]
                if len(survivors) >= MIN_CASES and len(survivors) < len(kept):
                    dropped += [c | {"why": "fails on base by calling the API wrongly"}
                                for i, c in enumerate(kept) if f"test_repro_{i}" in names]
                    kept = survivors
                    result = base_run(source, base_sha, imports, kept, workdir, label + "_trimmed")
                    record["base_run"] = result.to_dict()
                    miscalled = [c for c in result.cases if c["outcome"] == "failure" and not reproduces(c)]
            if miscalled:
                why = f"{miscalled[0]['call']}: {miscalled[0]['message']}"
                record["problem"] = "test is broken: " + why[:120]
                feedback = ("Some cases fail on their own errors (calling the API wrongly, unknown names, "
                            f"results that are not plain JSON) rather than on the bug:\n{_outcomes(result)}")
                continue
            if result.outcome == "fail" and result.failed >= 1:
                oracle.valid = True
                oracle.imports, oracle.cases, oracle.dropped = imports, kept, dropped
                oracle.test_code = render(imports, kept)
                oracle.test_sha256 = hashlib.sha256(oracle.test_code.encode()).hexdigest()
                break
            if result.outcome == "pass":
                feedback = "Every case passed on the current buggy code, so they do not reproduce the bug."
            else:
                feedback = (f"The cases did not run cleanly ({result.problem or 'no results'}).\n\n"
                            f"Output:\n{result.output_tail}")
        annotate(span, valid=oracle.valid, attempts=len(oracle.attempts),
                 cases=len(oracle.cases), dropped=oracle.dropped)
    return oracle
