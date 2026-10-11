"""Runs pytest on untrusted PR code inside Docker: no network, read-only source
mount, read-only root filesystem, CPU, memory and process limits.
"""

import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from linus.tracing import annotate, step

IMAGE = "linus-sandbox"
TIMEOUT_S = 120
SUMMARY = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)")


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

    @property
    def outcome(self) -> str:
        """pass, fail (tests ran and some failed), or error (did not run cleanly)."""
        if self.timed_out or self.errors or self.exit_code not in (0, 1):
            return "error"
        return "pass" if self.exit_code == 0 else "fail"

    def to_dict(self) -> dict:
        return asdict(self) | {"outcome": self.outcome}


def image_id() -> str:
    out = subprocess.run(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _parse(output: str) -> tuple[int, int, int]:
    counts = {"passed": 0, "failed": 0, "errors": 0}
    tail = output.strip().splitlines()[-1] if output.strip() else ""
    for n, word in SUMMARY.findall(tail):
        key = "errors" if word.startswith("error") else word
        if key in counts:
            counts[key] += int(n)
    return counts["passed"], counts["failed"], counts["errors"]


def _make_readable(tree: Path) -> None:
    for root, dirs, files in os.walk(tree):
        os.chmod(root, 0o755)
        for name in files:
            os.chmod(os.path.join(root, name), 0o644)


def pytest(tree: Path, targets: list[str], label: str) -> RunResult:
    """Run `pytest targets` against the source tree at `tree`."""
    _make_readable(tree)
    name = f"linus-{uuid.uuid4().hex[:12]}"
    pytest_args = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                   "--import-mode=importlib", "--no-header", "-rfE", *targets]
    cmd = [
        "docker", "run", "--rm", "--name", name,
        "--network", "none", "--memory", "512m", "--cpus", "1", "--pids-limit", "256",
        "--read-only", "--tmpfs", "/tmp", "--security-opt", "no-new-privileges",
        "-v", f"{tree.resolve()}:/work:ro", "-w", "/work", IMAGE, *pytest_args,
    ]
    with step(f"docker.pytest.{label}", kind="TOOL", targets=targets) as span:
        started = time.monotonic()
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)
            output, code, timed_out = out.stdout + out.stderr, out.returncode, False
        except subprocess.TimeoutExpired as exc:
            subprocess.run(["docker", "kill", name], capture_output=True)
            output, code, timed_out = str(exc.stdout or ""), -1, True
        passed, failed, errors = _parse(output)
        summary_lines = [line for line in output.splitlines()
                         if line.startswith(("FAILED ", "ERROR ")) and len(line.split()) > 1]
        failed_tests = [line.split()[1].split("::")[-1] for line in summary_lines]
        failures = [line.split(" - ", 1)[1] if " - " in line else line for line in summary_lines]
        result = RunResult(
            label=label, command=" ".join(pytest_args), exit_code=code,
            passed=passed, failed=failed, errors=errors,
            duration_s=round(time.monotonic() - started, 2), timed_out=timed_out,
            output_tail="\n".join(output.strip().splitlines()[-25:]),
            failed_tests=failed_tests, failures=failures,
        )
        annotate(span, exit_code=code, outcome=result.outcome, passed=passed, failed=failed, errors=errors)
    return result
