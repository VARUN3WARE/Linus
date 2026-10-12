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


def _run(code: str) -> dict[str, str]:
    """Execute a rendered oracle in-process: test name -> 'passed' or the failure."""
    namespace: dict = {}
    exec(compile(code, "test_linus_repro.py", "exec"), namespace)
    results = {}
    for name, fn in namespace.items():
        if name.startswith("test_"):
            try:
                fn()
                results[name] = "passed"
            except BaseException as exc:  # noqa: BLE001 - pytest.raises failures are BaseException
                results[name] = f"{type(exc).__name__}: {exc}"
    return results


def _code(*cases) -> str:
    text = json.dumps({"imports": ["import fakepkg"], "cases": [
        {"call": call, "expected": expected, "raises": raises} for call, expected, raises in cases]})
    imports, parsed = _parse_cases(text)
    return render(imports, parsed)


def test_strict_oracle_rejects_an_always_equal_result(fakepkg):
    fakepkg.f = lambda: AlwaysEqual(5)
    result = _run(_code(("fakepkg.f()", 2, None)))
    assert result["test_repro_0"].startswith("AssertionError: linus: result is a AlwaysEqual")


def test_strict_oracle_compares_exact_json(fakepkg):
    fakepkg.f = lambda: {"b": [1, "x", None], "a": True}
    fakepkg.g = lambda: 1
    fakepkg.h = lambda: (1, 2)
    fakepkg.k = lambda: {1: "x"}
    fakepkg.boom = lambda: int("x")
    result = _run(_code(("fakepkg.f()", {"a": True, "b": [1, "x", None]}, None),
                        ("fakepkg.g()", True, None),
                        ("fakepkg.h()", [1, 2], None),
                        ("fakepkg.k()", {"1": "x"}, None),
                        ("fakepkg.boom()", None, "ValueError")))
    assert result["test_repro_0"] == "passed"
    assert result["test_repro_1"].startswith("AssertionError")  # 1 is not true
    assert "not a plain JSON value" in result["test_repro_2"]  # tuple
    assert "not a plain JSON value" in result["test_repro_3"]  # int key
    assert result["test_repro_4"] == "passed"


def test_case_count_counts_rendered_tests(fakepkg):
    code = _code(("fakepkg.f()", 1, None), ("fakepkg.f()", 2, None))
    assert Oracle(issue=1, valid=True, test_code=code).case_count == 2


@pytest.mark.parametrize("case, real", [
    ({"message": "AssertionError: 2 != 3", "where": "test_linus_repro.py"}, True),
    ({"message": "Failed: DID NOT RAISE <class 'ValueError'>", "where": "test_linus_repro.py"}, True),
    ({"message": "TypeError: unsupported operand", "where": "/work/src/pkg/core.py"}, True),
    ({"message": "TypeError: f() got an unexpected keyword argument 'x'", "where": "test_linus_repro.py"}, False),
    ({"message": "TypeError: f() missing 1 required positional argument: 'n'", "where": "test_linus_repro.py"},
     False),
    ({"message": "TypeError: f() takes 1 positional argument but 2 were given", "where": "/work/src/a.py"},
     False),
    ({"message": "AttributeError: module 'pkg' has no attribute 'g'", "where": "test_linus_repro.py"}, False),
    ({"message": "TypeError: bad operand", "where": "test_linus_repro.py"}, False),
    ({"message": "AssertionError: linus: result is a tuple, not a plain JSON value", "where": ""}, False),
])
def test_only_real_reproductions_count(case, real):
    assert reproduces(case) is real


def _result(cases: list[tuple[str, str, str, str]]) -> RunResult:
    rows = [{"name": n, "outcome": o, "message": m, "where": w, "both": False} for n, o, m, w in cases]
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

    def fake_base_run(source, base_sha, code, workdir, label):
        runs.append(code)
        if len(runs) == 1:
            return _result([("test_repro_0", "failure", "AssertionError: 1 != 2", "test_linus_repro.py"),
                            ("test_repro_1", "passed", "", ""),
                            ("test_repro_2", "failure", "TypeError: f() got an unexpected keyword argument 'x'",
                             "test_linus_repro.py")])
        return _result([("test_repro_0", "failure", "AssertionError: 1 != 2", "test_linus_repro.py"),
                        ("test_repro_1", "passed", "", "")])

    monkeypatch.setattr(oracle_mod, "base_run", fake_base_run)
    monkeypatch.setattr(oracle_mod, "_context", lambda *a: ("", ""))
    gen = FakeGen([REPLY])
    built = oracle_mod.build(gen, None, None, Issue(1, "t", "b"), "base", set(), tmp_path)
    assert built.valid and gen.calls == 1
    assert [c["call"] for c in built.cases] == ["f(1)", "f(2)"]
    assert built.dropped[0]["call"] == "f(1, x=1)"
    assert "x=1" not in built.test_code and built.test_code == runs[-1]


def test_a_reproduction_made_only_of_miscalls_is_an_authoring_error(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "image_id", lambda: "img")
    monkeypatch.setattr(oracle_mod, "audit", lambda jev, issue, cases: [0.9] * len(cases))
    miscall = ("failure", "TypeError: f() missing 1 required positional argument: 'n'", "test_linus_repro.py")
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
