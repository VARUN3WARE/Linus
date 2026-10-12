import json
import sys
import types

import pytest

from linus import oracle as oracle_mod
from linus import sandbox
from linus.oracle import Oracle, _parse_cases, render, reproduces
from linus.sandbox import RunResult
from linus.source import Issue


class AlwaysEqual(int):
    def __eq__(self, other):
        return True

    __hash__ = int.__hash__


@pytest.fixture
def fakepkg(monkeypatch):
    module = types.ModuleType("fakepkg")
    module.answer = {"value": 2}
    monkeypatch.setitem(sys.modules, "fakepkg", module)
    return module


CASE_SCRIPT = None


def _case(tmp_path, source: str, call: str) -> dict:
    """Run the real per-call script on a throwaway package, as the container does."""
    import subprocess

    pkg = tmp_path / "pkgroot"
    (pkg / "fakepkg").mkdir(parents=True, exist_ok=True)
    (pkg / "fakepkg" / "__init__.py").write_text(source)
    script = tmp_path / "linus_case.py"
    script.write_text(sandbox.CASE)
    out = subprocess.run([sys.executable, "-I", "-B", str(script), str(pkg), json.dumps(["import fakepkg"]),
                          call], capture_output=True, text=True, timeout=30)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_always_equal_result_is_not_plain_json(tmp_path):
    source = "class AlwaysEqual(int):\n    def __eq__(self, other):\n        return True\n\n" \
             "    __hash__ = int.__hash__\n\n\ndef f():\n    return AlwaysEqual(5)\n"
    result = _case(tmp_path, source, "fakepkg.f()")
    assert result["ok"] and result["plain"] is False
    kind, _ = oracle_mod.check({"expected": 2, "raises": None}, result)
    assert kind == "not_plain"


def test_host_compares_exact_json():
    def judged(expected, value, plain=True, raises=None):
        return oracle_mod.check({"expected": expected, "raises": raises},
                                {"ok": True, "value": value, "plain": plain})[0]

    assert judged({"a": True, "b": [1, "x", None]}, {"b": [1, "x", None], "a": True}) == "passed"
    assert judged(True, 1) == "mismatch"  # JSON keeps true and 1 apart
    assert judged([1, 2], [1, 2], plain=False) == "not_plain"  # e.g. a tuple
    assert oracle_mod.check({"expected": None, "raises": "ValueError"},
                            {"ok": False, "error": "ValueError: bad", "where": "/work/src/p.py"})[0] == "passed"
    assert oracle_mod.check({"expected": 1, "raises": None},
                            {"ok": False, "error": "linus: no result", "where": "", "missing": True})[0] == "missing"


def test_case_count_counts_cases():
    cases = [{"call": "f()", "expected": 1, "raises": None}, {"call": "f()", "expected": 2, "raises": None}]
    assert Oracle(issue=1, valid=True, cases=cases).case_count == 2


@pytest.mark.parametrize("case, real", [
    ({"kind": "mismatch", "message": "2 != 3", "where": "", "call": "f(1)"}, True),
    ({"kind": "no_raise", "message": "DID NOT RAISE ValueError", "where": "", "call": "f(1)"}, True),
    ({"kind": "error", "message": "TypeError: unsupported operand", "where": "/work/src/pkg/core.py",
      "call": "f(1)"}, True),
    ({"kind": "error", "message": "TypeError: f() got an unexpected keyword argument 'x'", "where": "<linus-case>",
      "call": "f(1, x=1)"}, False),
    ({"kind": "error", "message": "TypeError: f() takes 1 positional argument but 2 were given",
      "where": "/work/src/a.py", "call": "f(1, 2)"}, False),
    ({"kind": "error", "message": "AttributeError: module 'pkg' has no attribute 'g'", "where": "<linus-case>",
      "call": "pkg.g()"}, False),
    ({"kind": "error", "message": "TypeError: bad operand", "where": "/usr/lib/python3.12/re/__init__.py",
      "call": "f(1)"}, False),
    ({"kind": "not_plain", "message": "result is not a plain JSON value", "where": "", "call": "f(1)"}, False),
])
def test_only_real_reproductions_count(case, real):
    assert reproduces(case) is real


def _kind(outcome: str, message: str) -> str:
    if outcome == "passed":
        return "passed"
    return "mismatch" if message.startswith("AssertionError") else "error"


def _result(cases: list[tuple[str, str, str, str]], calls=("f(1)", "f(2)", "f(1, x=1)")) -> RunResult:
    rows = [{"name": n, "outcome": o, "message": m, "where": w, "both": False, "kind": _kind(o, m),
             "call": calls[i] if i < len(calls) else "f()"} for i, (n, o, m, w) in enumerate(cases)]
    failed = sum(r["outcome"] == "failure" for r in rows)
    return RunResult(label="t", command="", exit_code=1 if failed else 0,
                     passed=sum(r["outcome"] == "passed" for r in rows), failed=failed, errors=0,
                     duration_s=0, timed_out=False, output_tail="", failed_tests=[], failures=[], cases=rows)


class FakeGen:
    model = "fake"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def complete(self, system, user):
        self.calls += 1
        return types.SimpleNamespace(text=self.replies.pop(0), prompt_tokens=1, completion_tokens=1)


REPLY = json.dumps({"imports": ["from pkg import f"], "cases": [
    {"call": "f(1)", "expected": 2}, {"call": "f(2)", "expected": 3}, {"call": "f(1, x=1)", "expected": 2}]})


def test_cases_that_call_the_api_wrongly_are_dropped(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "image_id", lambda: "img")
    monkeypatch.setattr(oracle_mod, "audit", lambda jev, issue, cases: [0.9] * len(cases))
    runs = []

    def fake_base_run(source, base_sha, imports, cases, workdir, label):
        runs.append([c["call"] for c in cases])
        if len(runs) == 1:
            return _result([("test_repro_0", "failure", "AssertionError: 1 != 2", "test_linus_repro.py"),
                            ("test_repro_1", "passed", "", ""),
                            ("test_repro_2", "failure", "TypeError: f() got an unexpected keyword argument 'x'",
                             "<linus-case>")])
        return _result([("test_repro_0", "failure", "AssertionError: 1 != 2", "test_linus_repro.py"),
                        ("test_repro_1", "passed", "", "")])

    monkeypatch.setattr(oracle_mod, "base_run", fake_base_run)
    monkeypatch.setattr(oracle_mod, "_context", lambda *a: ("", ""))
    gen = FakeGen([REPLY])
    built = oracle_mod.build(gen, None, None, Issue(1, "t", "b"), "base", set(), tmp_path)
    assert built.valid and gen.calls == 1
    assert [c["call"] for c in built.cases] == ["f(1)", "f(2)"]
    assert built.dropped[0]["call"] == "f(1, x=1)"
    assert "x=1" not in built.test_code and runs[-1] == ["f(1)", "f(2)"]


def test_a_reproduction_made_only_of_miscalls_is_an_authoring_error(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "image_id", lambda: "img")
    monkeypatch.setattr(oracle_mod, "audit", lambda jev, issue, cases: [0.9] * len(cases))
    miscall = ("failure", "TypeError: f() missing 1 required positional argument: 'n'", "<linus-case>")
    monkeypatch.setattr(oracle_mod, "base_run", lambda *a: _result(
        [(f"test_repro_{i}", *miscall) for i in range(3)]))
    monkeypatch.setattr(oracle_mod, "_context", lambda *a: ("", ""))
    gen = FakeGen([REPLY] * oracle_mod.MAX_ATTEMPTS)
    built = oracle_mod.build(gen, None, None, Issue(1, "t", "b"), "base", set(), tmp_path)
    assert not built.valid
    assert built.attempts[0]["problem"].startswith("test is broken")


def test_docker_failure_spends_no_generation(monkeypatch, tmp_path):
    def broken():
        raise sandbox.InfrastructureError("Cannot connect to the Docker daemon")

    monkeypatch.setattr(sandbox, "image_id", broken)
    gen = FakeGen([REPLY])
    with pytest.raises(sandbox.InfrastructureError):
        oracle_mod.build(gen, None, None, Issue(1, "t", "b"), "base", set(), tmp_path)
    assert gen.calls == 0
