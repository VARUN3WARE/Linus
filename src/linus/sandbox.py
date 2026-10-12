"""Runs untrusted PR code inside Docker: no network, read-only source mount,
read-only root filesystem, CPU, memory and process limits.

Two kinds of run:

- pytest runs (the PR's own tests, the base branch's tests). The PR's code runs
  in the same process as pytest, so nothing pytest prints is trusted on its
  own: counts come from a JUnit report written to a Linus-owned mount, the
  exit code must agree, and so must the printed summary when there is one.
- oracle calls. The container gets only the imports and the call expressions,
  never the expected values. A Linus-owned driver runs each call in its own
  fresh interpreter and prints what came back; the host compares it with the
  expected value (see oracle.py). Patching anything inside the container can't
  forge a pass, because the answer it would have to match never enters it.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from linus.tracing import annotate, step

IMAGE = "linus-sandbox"
TIMEOUT_S = 120
CASE_TIMEOUT_S = 10  # per oracle call, each in its own interpreter
SUMMARY = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)")
SUMMARY_LINE = re.compile(r"=*\s*((\d+ \w+(, )?)+|no tests ran) in [\d.]+s\b.*")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
WHERE = re.compile(r"^(\S+):\d+: \w+", re.M)
SRC_MOUNT, ORACLE_MOUNT, OUT_MOUNT = "/work", "/linus-oracle", "/linus-out"
ORACLE_FILE = "test_linus_repro.py"  # the oracle's readable rendering (receipts); never run
DRIVER_FILE, CASE_FILE, CALLS_FILE = "linus_driver.py", "linus_case.py", "calls.json"
REPORT = "report.xml"
# The PR's import paths go on the end of sys.path after pytest is imported, so a
# PR file named pytest.py (or json.py) can't replace pytest or the stdlib, and
# python -I ignores PYTHON* variables, user site-packages and the working dir.
LAUNCH = "import sys, pytest; sys.path.extend(sys.argv.pop(1).split(':')); sys.exit(pytest.main(sys.argv[1:]))"
# After the project's addopts (pytest puts addopts first), so a project's -q or
# --color=yes can't hide or decorate the summary line.
OUTPUT_ARGS = ["--verbosity=-1", "--color=no", "-p", "no:cacheprovider"]
INFRA_EXIT_CODES = (125, 126, 127)
INFRA_MESSAGES = ("Cannot connect to the Docker daemon", "Is the docker daemon running", "No such image")

# One oracle call in a fresh interpreter. Linus's own imports come first and the
# PR's roots go on the end of sys.path. The PR's prints are moved to stderr so
# the one result line on stdout is ours, and os._exit skips the PR's atexit
# hooks. None of this is what keeps the verdict honest (the host compares
# against a value the container never sees); it keeps honest output parseable.
CASE = r'''import json, os, sys, traceback

_out = os.fdopen(os.dup(1), "w")
os.dup2(2, 1)
_JSON = (str, int, float, bool, list, dict, type(None))


def _plain(value, depth=0):
    """Exactly the plain JSON types, all the way down: no subclasses, str keys."""
    kind = type(value)
    if depth > 100 or not any(kind is t for t in _JSON):
        return False
    if kind is list:
        return all(_plain(item, depth + 1) for item in value)
    if kind is dict:
        return all(type(k) is str and _plain(v, depth + 1) for k, v in value.items())
    return True


def _emit(record):
    _out.write(json.dumps(record) + "\n")
    _out.flush()
    os._exit(0)


roots, imports, call = sys.argv[1].split(":"), json.loads(sys.argv[2]), sys.argv[3]
sys.path.extend(roots)
try:
    namespace = {"__name__": "__linus_case__"}
    exec(compile("\n".join(imports), "<linus-imports>", "exec"), namespace)
    value = eval(compile(call, "<linus-case>", "eval"), namespace)
except BaseException as exc:
    frames = traceback.extract_tb(exc.__traceback__)
    try:
        message = str(exc)
    except BaseException:
        message = "<unprintable>"
    _emit({"ok": False, "error": f"{type(exc).__name__}: {message}"[:500],
           "where": frames[-1].filename if frames else ""})
try:
    plain = _plain(value)
    json.dumps(value)
except BaseException:
    value, plain = repr(value)[:200], False
_emit({"ok": True, "value": value, "plain": plain})
'''

# Runs each call through CASE in its own interpreter, with a timeout, and prints
# one JSON line per call holding the raw result line. It decides nothing.
DRIVER = r'''import json, subprocess, sys

spec = json.load(open(sys.argv[1]))
sys.path.extend(spec["roots"])  # after the driver's own imports; it never imports the PR itself
case = sys.argv[1].rsplit("/", 1)[0] + "/" + spec["case_file"]
for i, call in enumerate(spec["calls"]):
    record = {"case": i}
    try:
        done = subprocess.run([sys.executable, "-I", "-B", case, ":".join(spec["roots"]),
                               json.dumps(spec["imports"]), call],
                              capture_output=True, text=True, errors="replace", timeout=spec["timeout"])
        lines = done.stdout.strip().splitlines()
        record.update(line=lines[-1] if lines else "", exit=done.returncode, stderr=done.stderr[-1500:])
    except subprocess.TimeoutExpired:
        record.update(line="", exit=None, timed_out=True, stderr="")
    print(json.dumps(record), flush=True)
'''


class InfrastructureError(RuntimeError):
    """Docker itself failed. Says nothing about the PR or the oracle."""


@dataclass
class RunResult:
    label: str
    command: str
    exit_code: int
    passed: int
    failed: int
    errors: int
    duration_s: float
    timed_out: bool
    output_tail: str
    failed_tests: list[str]
    failures: list[str]  # exception summaries, e.g. "AssertionError: assert 'a-b' == 'a--b'"
    problem: str = ""  # why the counts can't be trusted (missing report, report and summary disagree)
    cases: list[dict] = field(default_factory=list)  # per test: name, outcome, message, where

    @property
    def outcome(self) -> str:
        """pass, fail (tests ran and some failed), or error (did not run cleanly).

        A pass needs every signal to agree: exit code, failure and error counts,
        and at least one test that actually passed.
        """
        if self.timed_out or self.problem or self.errors or self.exit_code not in (0, 1):
            return "error"
        if self.exit_code == 0 and self.failed == 0 and self.passed > 0:
            return "pass"
        if self.exit_code == 1 and self.failed > 0:
            return "fail"
        return "error"

    def to_dict(self) -> dict:
        return asdict(self) | {"outcome": self.outcome}


@dataclass
class CallsRun:
    """Raw oracle results from the container, one per call, not yet judged."""
    results: list[dict]  # {"ok": True, "value", "plain"} | {"ok": False, "error", "where"[, "missing"]}
    exit_code: int
    timed_out: bool
    duration_s: float
    output_tail: str
    problem: str = ""


def image_id() -> str:
    try:
        out = subprocess.run(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"],
                             capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise InfrastructureError("docker is not installed") from exc
    if out.returncode != 0:
        raise InfrastructureError(f"sandbox image unavailable: {(out.stderr or out.stdout).strip()[:200]}")
    return out.stdout.strip()


def import_roots(files: list[str]) -> list[str]:
    """Where the package is imported from, decided by the BASE tree's layout
    (src/ or flat) and used for base and PR runs alike: a PR that adds a src/
    copy of the package must not get to choose which copy is tested."""
    src = any(f.startswith("src/") and f.endswith(".py") for f in files)
    return [f"{SRC_MOUNT}/src" if src else SRC_MOUNT]


def _tree_files(tree: Path) -> list[str]:
    return [p.relative_to(tree).as_posix() for p in tree.rglob("*.py")]


def _parse(output: str) -> tuple[int, int, int] | None:
    """passed (incl. xpassed, as JUnit counts them), failed, errors from the
    summary line, or None if there isn't one (missing evidence, not a zero)."""
    counts = {"passed": 0, "failed": 0, "errors": 0}
    lines = [ANSI.sub("", line).strip() for line in output.strip().splitlines()]
    tail = next((line for line in reversed(lines) if SUMMARY_LINE.fullmatch(line)), None)
    if tail is None:
        return None
    for n, word in SUMMARY.findall(tail):
        key = {"xpassed": "passed", "error": "errors"}.get(word, word)
        if key in counts:
            counts[key] += int(n)
    return counts["passed"], counts["failed"], counts["errors"]


def _junit(path: Path) -> list[dict] | None:
    """Per-test results from the JUnit report, or None if pytest never wrote one."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    cases = []
    for case in root.iter("testcase"):
        bad = {child.tag: child for child in case if child.tag in ("failure", "error", "skipped")}
        outcome = next((tag for tag in ("failure", "error", "skipped") if tag in bad), "passed")
        detail = bad.get("failure") if "failure" in bad else bad.get("error")
        where = WHERE.findall(detail.text or "") if detail is not None else []
        cases.append({"name": case.get("name", ""), "classname": case.get("classname", ""), "outcome": outcome,
                      "both": "failure" in bad and "error" in bad,
                      "message": (detail.get("message", "") if detail is not None else ""),
                      "where": where[-1] if where else ""})
    return cases


def _counts(cases: list[dict]) -> tuple[int, int, int]:
    passed = sum(c["outcome"] == "passed" for c in cases)
    failed = sum(c["outcome"] == "failure" for c in cases)
    errors = sum(c["outcome"] == "error" or c["both"] for c in cases)
    return passed, failed, errors


def _make_readable(tree: Path) -> None:
    """The container user must read the tree. Symlinks are skipped: chmod would
    follow them, and a dangling one would crash the run."""
    for root, dirs, files in os.walk(tree):
        os.chmod(root, 0o755)
        for name in files:
            path = os.path.join(root, name)
            if not os.path.islink(path):
                os.chmod(path, 0o644)


def _infra_failure(code: int, stderr: str, report: bool) -> str | None:
    """Docker's own failures, recognized only when the run left no evidence: PR
    code can exit 127 too, but it can't stop a report from having been written."""
    if report:
        return None
    if code in INFRA_EXIT_CODES:
        return f"docker exited {code}: {stderr.strip()[:200]}"
    if any(m in stderr for m in INFRA_MESSAGES):
        return stderr.strip()[:200]
    return None


def _decode(data) -> str:
    """TimeoutExpired carries bytes even when text=True was asked for."""
    if isinstance(data, bytes):
        return data.decode(errors="replace")
    return data or ""


def _docker(tree: Path, mounts: list[str], workdir: str, command: list[str], name: str,
            timeout: int) -> tuple[str, str, int, bool]:
    """stdout, stderr, exit code, timed out."""
    _make_readable(tree)
    cmd = [
        "docker", "run", "--rm", "--name", name,
        "--network", "none", "--memory", "512m", "--cpus", "1", "--pids-limit", "256",
        "--read-only", "--tmpfs", "/tmp", "--security-opt", "no-new-privileges",
        "-e", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        "-v", f"{tree.resolve()}:{SRC_MOUNT}:ro", *mounts, "-w", workdir, IMAGE, *command,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout, out.stderr, out.returncode, False
    except FileNotFoundError as exc:
        raise InfrastructureError("docker is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        subprocess.run(["docker", "kill", name], capture_output=True)
        return _decode(exc.stdout), _decode(exc.stderr), -1, True


def _pytest_result(label: str, args: list[str], code: int, stdout: str, stderr: str, timed_out: bool,
                   cases: list[dict] | None, seconds: float) -> RunResult:
    printed = _parse(stdout)
    problem = ""
    if cases is None:
        passed, failed, errors = printed or (0, 0, 0)
        problem = "pytest wrote no JUnit report"
    else:
        passed, failed, errors = _counts(cases)
        if printed is not None and printed != (passed, failed, errors):
            problem = (f"possible forgery: printed summary {printed} disagrees with the JUnit report "
                       f"{(passed, failed, errors)} (passed, failed, errors)")
    bad = [c for c in cases or [] if c["outcome"] in ("failure", "error")]
    return RunResult(
        label=label, command=" ".join(args), exit_code=code,
        passed=passed, failed=failed, errors=errors, duration_s=seconds, timed_out=timed_out,
        output_tail="\n".join((stdout + stderr).strip().splitlines()[-25:]),
        failed_tests=[c["name"] for c in bad],
        failures=[c["message"].splitlines()[0] if c["message"] else c["name"] for c in bad],
        problem=problem, cases=cases or [],
    )


def pytest(tree: Path, targets: list[str], label: str, roots: list[str] | None = None) -> RunResult:
    """Run `pytest targets` against the source tree at `tree`, with the tree's own
    pytest config (its test suite may need it). `roots` should come from the
    base tree (import_roots); it defaults to the tree's own layout."""
    out_dir = Path(tempfile.mkdtemp(prefix="out-", dir=tree.parent))
    try:
        os.chmod(out_dir, 0o777)  # the container user writes the JUnit report here
        roots = roots or import_roots(_tree_files(tree))
        # A fixed rootdir keeps JUnit classnames path-based from the tree root, so
        # the base tests' node ids can be checked against the report.
        args = [*OUTPUT_ARGS, "--import-mode=importlib", "--no-header", "-rfE", f"--rootdir={SRC_MOUNT}",
                *targets, f"--junitxml={OUT_MOUNT}/{REPORT}"]
        command = ["python", "-I", "-B", "-c", LAUNCH, ":".join(roots), *args]
        mounts = ["-v", f"{out_dir.resolve()}:{OUT_MOUNT}"]
        name = f"linus-{uuid.uuid4().hex[:12]}"
        with step(f"docker.pytest.{label}", kind="TOOL", targets=targets) as span:
            started = time.monotonic()
            stdout, stderr, code, timed_out = _docker(tree, mounts, SRC_MOUNT, command, name, TIMEOUT_S)
            cases = _junit(out_dir / REPORT)
            if why := _infra_failure(code, stderr, cases is not None):
                raise InfrastructureError(why)
            result = _pytest_result(label, args, code, stdout, stderr, timed_out, cases,
                                    round(time.monotonic() - started, 2))
            annotate(span, exit_code=code, outcome=result.outcome, passed=result.passed, failed=result.failed,
                     errors=result.errors, problem=result.problem)
        return result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _garbled(why: str) -> dict:
    return {"ok": False, "error": f"linus: {why}", "where": "", "missing": True}


def _child_result(record: dict | None, duplicated: bool) -> dict:
    """The result one call printed, checked for shape. Anything else is a failed call."""
    if record is None:
        return _garbled("no result from the driver")
    if duplicated:
        return _garbled("more than one result for this call")
    if record.get("timed_out"):
        return _garbled(f"timed out after {CASE_TIMEOUT_S}s")
    try:
        result = json.loads(record.get("line") or "")
    except (TypeError, ValueError):
        result = None
    if isinstance(result, dict) and result.get("ok") is True and "value" in result \
            and type(result.get("plain")) is bool:
        return {"ok": True, "value": result["value"], "plain": result["plain"]}
    if isinstance(result, dict) and result.get("ok") is False and isinstance(result.get("error"), str) \
            and isinstance(result.get("where"), str):
        return {"ok": False, "error": result["error"][:500], "where": result["where"]}
    stderr = (record.get("stderr") or "").strip().splitlines()
    return _garbled(f"no result line (exit {record.get('exit')})" + (f": {stderr[-1][:200]}" if stderr else ""))


def _driver_results(stdout: str, n: int) -> list[dict | None]:
    """Per call, the driver's record, in order; a call reported twice is garbled."""
    seen: dict[int, dict] = {}
    duplicated = set()
    for line in stdout.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or type(record.get("case")) is not int or not 0 <= record["case"] < n:
            continue
        if record["case"] in seen:
            duplicated.add(record["case"])
        seen[record["case"]] = record
    return [_child_result(seen.get(i), i in duplicated) for i in range(n)]


def run_calls(tree: Path, imports: list[str], calls: list[str], label: str, roots: list[str]) -> CallsRun:
    """Evaluate each oracle call against the code at `tree`, one fresh interpreter
    per call. Only imports and calls go in; raw results come out."""
    oracle_dir = Path(tempfile.mkdtemp(prefix="oracle-", dir=tree.parent))
    try:
        (oracle_dir / DRIVER_FILE).write_text(DRIVER)
        (oracle_dir / CASE_FILE).write_text(CASE)
        (oracle_dir / CALLS_FILE).write_text(json.dumps({"imports": imports, "calls": calls, "roots": roots,
                                                         "case_file": CASE_FILE, "timeout": CASE_TIMEOUT_S}))
        _make_readable(oracle_dir)
        command = ["python", "-I", "-B", f"{ORACLE_MOUNT}/{DRIVER_FILE}", f"{ORACLE_MOUNT}/{CALLS_FILE}"]
        mounts = ["-v", f"{oracle_dir.resolve()}:{ORACLE_MOUNT}:ro"]
        name = f"linus-{uuid.uuid4().hex[:12]}"
        with step(f"docker.oracle.{label}", kind="TOOL", calls=len(calls)) as span:
            started = time.monotonic()
            stdout, stderr, code, timed_out = _docker(tree, mounts, ORACLE_MOUNT, command, name,
                                                      TIMEOUT_S + CASE_TIMEOUT_S * len(calls))
            reported = any(line.startswith('{"case"') for line in stdout.splitlines())
            if why := _infra_failure(code, stderr, reported):
                raise InfrastructureError(why)
            problem = "" if code == 0 and not timed_out else f"oracle driver exited {code}"
            run = CallsRun(results=_driver_results(stdout, len(calls)), exit_code=code, timed_out=timed_out,
                           duration_s=round(time.monotonic() - started, 2),
                           output_tail="\n".join(stderr.strip().splitlines()[-25:]), problem=problem)
            annotate(span, exit_code=code, problem=problem)
        return run
    finally:
        shutil.rmtree(oracle_dir, ignore_errors=True)
