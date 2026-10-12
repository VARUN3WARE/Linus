"""Runs pytest on untrusted PR code inside Docker: no network, read-only source
mount, read-only root filesystem, CPU, memory and process limits.

The PR's code runs in the same process as pytest, so nothing pytest prints is
trusted on its own: counts come from a JUnit report written to a Linus-owned
mount and must agree with the printed summary. The oracle runs from a
Linus-owned directory with Linus's own config, so the PR's conftest.py and
pytest settings never load for it.
"""

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
SUMMARY = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)")
SUMMARY_LINE = re.compile(r"=*\s*((\d+ \w+(, )?)+|no tests ran) in [\d.]+s\b.*")
WHERE = re.compile(r"^(\S+):\d+: \w+", re.M)
SRC_MOUNT, ORACLE_MOUNT, OUT_MOUNT = "/work", "/linus-oracle", "/linus-out"
ORACLE_FILE = "test_linus_repro.py"
REPORT = "report.xml"
# The PR's import paths go on the end of sys.path after pytest is imported, so a
# PR file named pytest.py (or json.py) can't replace pytest or the stdlib, and
# python -I ignores PYTHON* variables, user site-packages and the working dir.
LAUNCH = "import sys, pytest; sys.path.extend(sys.argv.pop(1).split(':')); sys.exit(pytest.main(sys.argv[1:]))"
ORACLE_INI = "[pytest]\n"
INFRA_EXIT_CODES = (125, 126, 127)
INFRA_MESSAGES = ("Cannot connect to the Docker daemon", "No such image")


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


def image_id() -> str:
    try:
        out = subprocess.run(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"],
                             capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise InfrastructureError("docker is not installed") from exc
    if out.returncode != 0:
        raise InfrastructureError(f"sandbox image unavailable: {(out.stderr or out.stdout).strip()[:200]}")
    return out.stdout.strip()


def _parse(output: str) -> tuple[int, int, int]:
    """passed (incl. xpassed, as JUnit counts them), failed, errors from the summary line."""
    counts = {"passed": 0, "failed": 0, "errors": 0}
    lines = [line.strip() for line in output.strip().splitlines()]
    tail = next((line for line in reversed(lines) if SUMMARY_LINE.fullmatch(line)), "")
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
        cases.append({"name": case.get("name", ""), "outcome": outcome,
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


def _import_paths(tree: Path) -> str:
    return f"{SRC_MOUNT}/src" if (tree / "src").is_dir() else SRC_MOUNT


def _infra_failure(code: int, stderr: str, report: bool) -> str | None:
    if code in INFRA_EXIT_CODES:
        return f"docker exited {code}: {stderr.strip()[:200]}"
    if not report and any(m in stderr for m in INFRA_MESSAGES):
        return stderr.strip()[:200]
    return None


def _decode(data) -> str:
    """TimeoutExpired carries bytes even when text=True was asked for."""
    if isinstance(data, bytes):
        return data.decode(errors="replace")
    return data or ""


def _run(tree: Path, mounts: list[str], workdir: str, pytest_args: list[str], label: str,
         targets: list[str], out_dir: Path) -> RunResult:
    _make_readable(tree)
    os.chmod(out_dir, 0o777)  # the container user writes the JUnit report here
    name = f"linus-{uuid.uuid4().hex[:12]}"
    args = [*pytest_args, f"--junitxml={OUT_MOUNT}/{REPORT}"]
    cmd = [
        "docker", "run", "--rm", "--name", name,
        "--network", "none", "--memory", "512m", "--cpus", "1", "--pids-limit", "256",
        "--read-only", "--tmpfs", "/tmp", "--security-opt", "no-new-privileges",
        "-e", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        "-v", f"{tree.resolve()}:{SRC_MOUNT}:ro", *mounts, "-v", f"{out_dir.resolve()}:{OUT_MOUNT}",
        "-w", workdir, IMAGE, "python", "-I", "-B", "-c", LAUNCH, _import_paths(tree), *args,
    ]
    with step(f"docker.pytest.{label}", kind="TOOL", targets=targets) as span:
        started = time.monotonic()
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)
            stdout, stderr, code, timed_out = out.stdout, out.stderr, out.returncode, False
        except FileNotFoundError as exc:
            raise InfrastructureError("docker is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            subprocess.run(["docker", "kill", name], capture_output=True)
            stdout, stderr, code, timed_out = _decode(exc.stdout), _decode(exc.stderr), -1, True
        cases = _junit(out_dir / REPORT)
        if why := _infra_failure(code, stderr, cases is not None):
            raise InfrastructureError(why)
        output = stdout + stderr
        printed = _parse(stdout)
        problem = ""
        if cases is None:
            passed, failed, errors = printed
            problem = "pytest wrote no JUnit report"
        else:
            passed, failed, errors = _counts(cases)
            if printed != (passed, failed, errors):
                problem = (f"possible forgery: printed summary {printed} disagrees with the JUnit report "
                           f"{(passed, failed, errors)} (passed, failed, errors)")
        bad = [c for c in cases or [] if c["outcome"] in ("failure", "error")]
        result = RunResult(
            label=label, command=" ".join(args), exit_code=code,
            passed=passed, failed=failed, errors=errors,
            duration_s=round(time.monotonic() - started, 2), timed_out=timed_out,
            output_tail="\n".join(output.strip().splitlines()[-25:]),
            failed_tests=[c["name"] for c in bad], failures=[c["message"].splitlines()[0] if c["message"]
                                                            else c["name"] for c in bad],
            problem=problem, cases=cases or [],
        )
        annotate(span, exit_code=code, outcome=result.outcome, passed=passed, failed=failed, errors=errors,
                 problem=problem)
    return result


def pytest(tree: Path, targets: list[str], label: str) -> RunResult:
    """Run `pytest targets` against the source tree at `tree`, with the PR's own
    pytest config (its test suite may need it)."""
    out_dir = Path(tempfile.mkdtemp(prefix="out-", dir=tree.parent))
    try:
        args = ["-q", "-p", "no:cacheprovider", "--import-mode=importlib", "--no-header", "-rfE", *targets]
        return _run(tree, [], SRC_MOUNT, args, label, targets, out_dir)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def run_oracle(tree: Path, test_code: str, label: str) -> RunResult:
    """Run the oracle test against the code at `tree`, outside the PR's tree:
    Linus's own ini and rootdir, no conftest files, no cache."""
    oracle_dir = Path(tempfile.mkdtemp(prefix="oracle-", dir=tree.parent))
    out_dir = Path(tempfile.mkdtemp(prefix="out-", dir=tree.parent))
    try:
        (oracle_dir / ORACLE_FILE).write_text(test_code)
        (oracle_dir / "pytest.ini").write_text(ORACLE_INI)
        _make_readable(oracle_dir)
        args = ["-q", "-p", "no:cacheprovider", "--noconftest", "-c", f"{ORACLE_MOUNT}/pytest.ini",
                f"--rootdir={ORACLE_MOUNT}", "--import-mode=importlib", "--no-header", "-rfE",
                f"{ORACLE_MOUNT}/{ORACLE_FILE}"]
        mounts = ["-v", f"{oracle_dir.resolve()}:{ORACLE_MOUNT}:ro"]
        return _run(tree, mounts, ORACLE_MOUNT, args, label, [ORACLE_FILE], out_dir)
    finally:
        shutil.rmtree(oracle_dir, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)
