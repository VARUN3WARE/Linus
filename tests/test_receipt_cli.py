"""`linus receipt` rebuilds the oracle from the receipt and re-runs it with the
current call signatures (v6 broke this once, and no test ran the command)."""

import json
import types

from typer.testing import CliRunner

from linus import cli
from linus import oracle as oracle_mod
from linus import sandbox
from linus import verify as verify_mod
from linus.oracle import FORMAT, render


def test_receipt_rebuilds_the_oracle_and_reproduces(tmp_path, monkeypatch):
    imports = ["from textkit import chunk"]
    cases = [{"call": "chunk([1, 2, 3], 2)", "expected": [[1, 2], [3]], "raises": None, "from_issue": True}]
    code = render(imports, cases)
    import hashlib
    receipt = {
        "repo": "demo/textkit", "period": "2026-09", "verdict": "pay", "reason": "fails on base, passes on PR",
        "pr": {"number": 101, "author": "alice-dev", "base_sha": "b", "head_sha": "h"},
        "sandbox_image": "img", "signals": {},
        "oracle": {"issue": 1, "valid": True, "format": FORMAT, "test_path": "t.py", "model": "m",
                   "test_code": code, "test_sha256": hashlib.sha256(code.encode()).hexdigest(),
                   "imports": imports, "cases": cases, "dropped": [], "attempts": []},
        "verification": {"runs": [{"label": "pr101_oracle", "outcome": "pass", "passed": 1, "failed": 0}]},
    }
    path = tmp_path / "pr-101.json"
    path.write_text(json.dumps(receipt))

    pr = types.SimpleNamespace(number=101, base_sha="b", head_sha="h")
    source = types.SimpleNamespace(name="demo/textkit", pulls=lambda: [pr])
    monkeypatch.setattr(cli, "_source", lambda *a, **k: source)
    monkeypatch.setattr(sandbox, "image_id", lambda: "img")
    seen = {}

    def fake_base_run(src, base_sha, imp, cs, workdir, label):
        seen["base"] = (imp, cs)
        return types.SimpleNamespace(outcome="fail")

    def fake_verify(src, p, oracle, signals, workdir):
        seen["oracle"] = oracle
        return types.SimpleNamespace(runs=[{"label": "pr101_oracle", "outcome": "pass", "passed": 1, "failed": 0}])

    monkeypatch.setattr(oracle_mod, "base_run", fake_base_run)
    monkeypatch.setattr(verify_mod, "verify", fake_verify)
    monkeypatch.setenv("LINUS_STATE_DIR", str(tmp_path / "state"))

    result = CliRunner().invoke(cli.app, ["receipt", str(path)])
    assert result.exit_code == 0, result.output
    assert "Reproduced" in result.output
    assert seen["base"] == (imports, cases)
    assert seen["oracle"].cases == cases and seen["oracle"].imports == imports
