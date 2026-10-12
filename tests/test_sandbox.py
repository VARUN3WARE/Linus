from pathlib import Path
import json
"""Sandbox result handling, offline: Docker is replaced by a fake subprocess."""

import subprocess
import types

import pytest

from linus import sandbox
from linus.sandbox import RunResult


def _result(exit_code=0, passed=1, failed=0, errors=0, problem="", timed_out=False) -> RunResult:
    return RunResult(label="t", command="", exit_code=exit_code, passed=passed, failed=failed, errors=errors,
                     duration_s=0, timed_out=timed_out, output_tail="", failed_tests=[], failures=[],
                     problem=problem)


@pytest.mark.parametrize("kwargs, outcome", [
    ({}, "pass"),
    ({"exit_code": 0, "failed": 2}, "error"),  # exit status forced to 0 despite failures
    ({"exit_code": 0, "passed": 0}, "error"),  # nothing actually passed
    ({"exit_code": 0, "errors": 1}, "error"),
    ({"exit_code": 1, "failed": 1}, "fail"),
    ({"exit_code": 1, "failed": 0}, "error"),
    ({"exit_code": 5, "passed": 0}, "error"),
    ({"problem": "possible forgery"}, "error"),
    ({"timed_out": True}, "error"),
])
def test_outcome_needs_every_signal_to_agree(kwargs, outcome):
    assert _result(**kwargs).outcome == outcome


def test_summary_is_read_from_the_last_summary_line():
    out = "3 passed in 0.01s\nFAILED x\n1 failed, 2 passed, 1 xpassed, 1 warning in 0.02s\nnoise after\n"
    assert sandbox._parse(out) == (3, 1, 0)


JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite>
<testcase classname="t" name="test_a" />
<testcase classname="t" name="test_b"><failure message="AssertionError: 1 != 2">x
test_linus_repro.py:5: AssertionError</failure></testcase>
<testcase classname="t" name="test_c"><failure message="TypeError: bad">x
/work/src/pkg/core.py:9: TypeError</failure></testcase>
<testcase classname="t" name="test_d"><skipped message="xfail" /></testcase>
<testcase classname="t" name="test_e"><error message="fixture 'y' not found">x</error></testcase>
</testsuite></testsuites>"""


def test_junit_report_gives_per_test_outcomes(tmp_path):
    path = tmp_path / "report.xml"
    path.write_text(JUNIT)
    cases = sandbox._junit(path)
    assert sandbox._counts(cases) == (1, 2, 1)
    assert [c["where"] for c in cases[1:3]] == ["test_linus_repro.py", "/work/src/pkg/core.py"]
    assert sandbox._junit(tmp_path / "missing.xml") is None


def _fake_docker(monkeypatch, returncode=0, stdout="", stderr="", report=None, timeout=False):
    def run(cmd, **kwargs):
        if cmd[:2] == ["docker", "kill"]:
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        out = next(part.split(":")[0] for part in cmd if part.endswith(":/linus-out"))
        if report is not None:
            with open(f"{out}/report.xml", "w") as f:
                f.write(report)
        if timeout:
            raise subprocess.TimeoutExpired(cmd, 1, output=stdout.encode(), stderr=stderr.encode())
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(sandbox.subprocess, "run", run)


ONE_PASS = '<testsuites><testsuite><testcase name="test_a" /></testsuite></testsuites>'


def test_counts_come_from_the_junit_report(monkeypatch, tmp_path):
    _fake_docker(monkeypatch, stdout="1 passed in 0.01s\n", report=ONE_PASS)
    (tmp_path / "tree").mkdir()
    result = sandbox.pytest(tmp_path / "tree", ["tests"], "t")
    assert result.outcome == "pass" and result.passed == 1


def test_summary_that_disagrees_with_the_report_is_an_error(monkeypatch, tmp_path):
    _fake_docker(monkeypatch, stdout="5 passed in 0.01s\n", report=ONE_PASS)
    (tmp_path / "tree").mkdir()
    result = sandbox.pytest(tmp_path / "tree", ["tests"], "t")
    assert result.outcome == "error" and "forgery" in result.problem


def test_missing_report_is_an_error(monkeypatch, tmp_path):
    _fake_docker(monkeypatch, stdout="1 passed in 0.01s\n")
    (tmp_path / "tree").mkdir()
    assert sandbox.pytest(tmp_path / "tree", ["tests"], "t").outcome == "error"


def test_oracle_calls_go_in_and_answers_stay_on_the_host(monkeypatch, tmp_path):
    seen = {}

    def run(cmd, **kwargs):
        mount = next(p for p in cmd if p.endswith(":/linus-oracle:ro")).split(":")[0]
        seen["cmd"], seen["spec"] = cmd, json.loads(Path(mount, "calls.json").read_text())
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", run)
    (tmp_path / "tree" / "src").mkdir(parents=True)
    sandbox.run_calls(tmp_path / "tree", ["import pkg"], ["pkg.f(1)"], "t", ["/work/src"])
    cmd, spec = seen["cmd"], seen["spec"]
    assert any(p.endswith(":/work:ro") for p in cmd) and "-I" in cmd
    assert set(spec) == {"imports", "calls", "roots", "case_file", "timeout"}
    assert "expected" not in json.dumps(spec)


@pytest.mark.parametrize("returncode, stderr", [
    (125, "docker: Error response from daemon"),
    (1, "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"),
    (127, ""),
])
def test_docker_failures_are_infrastructure_errors(monkeypatch, tmp_path, returncode, stderr):
    _fake_docker(monkeypatch, returncode=returncode, stderr=stderr)
    (tmp_path / "tree").mkdir()
    with pytest.raises(sandbox.InfrastructureError):
        sandbox.pytest(tmp_path / "tree", ["tests"], "t")


def test_timeout_output_is_decoded(monkeypatch, tmp_path):
    _fake_docker(monkeypatch, stdout="still running\n", timeout=True)
    (tmp_path / "tree").mkdir()
    result = sandbox.pytest(tmp_path / "tree", ["tests"], "t")
    assert result.timed_out and result.outcome == "error"
    assert "still running" in result.output_tail and "b'" not in result.output_tail


def test_make_readable_skips_symlinks(tmp_path):
    (tmp_path / "file.py").write_text("x")
    (tmp_path / "dangling").symlink_to(tmp_path / "missing")
    (tmp_path / "outside").symlink_to("/etc/hostname")
    sandbox._make_readable(tmp_path)  # must not raise or follow the links
    assert oct((tmp_path / "file.py").stat().st_mode)[-3:] == "644"
