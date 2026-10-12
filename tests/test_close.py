"""The close's bookkeeping, offline: no JEV, OpenAI or Docker calls."""

import json
import types
from dataclasses import asdict
from pathlib import Path

import pytest

from linus import close as close_mod
from linus import ledger, sandbox
from linus.config import Settings
from linus.diffcheck import DiffSignals
from linus.oracle import Oracle
from linus.policy import load as load_policy
from linus.source import PullRequest
from linus.verify import Verification

POLICY = load_policy(Path(__file__).parents[1] / "bounty.yml")


class UI:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class Source:
    name, period = "demo/x", "2026-09"

    def __init__(self, pulls, diffs=None):
        self._pulls, self.diffs, self.issues = pulls, diffs or {}, {}

    def pulls(self):
        return self._pulls

    def diff(self, pr):
        return self.diffs.get(pr.number, "")


def _pr(number: int, author: str = "a", issue: int = 1) -> PullRequest:
    return PullRequest(number=number, title=f"PR {number}", body=f"Fixes #{issue}", author=author,
                       created_at=f"2026-09-{number % 28 + 1:02d}T00:00:00Z", base_sha="base",
                       head_sha=f"head{number}", issue_numbers=(issue,))


def _close(tmp_path, monkeypatch, pulls=(), diffs=None, run_id=None) -> close_mod.Close:
    monkeypatch.setattr(close_mod, "Jev", lambda settings: types.SimpleNamespace(input_tokens=0))
    monkeypatch.setattr(close_mod, "Generator",
                        lambda settings: types.SimpleNamespace(prompt_tokens=0, completion_tokens=0))
    monkeypatch.setattr(sandbox, "image_id", lambda: "sha256:img")
    settings = Settings("", "jev", "", "", "gpt-5.4-mini", "", "", "linus", "", "", "", tmp_path)
    return close_mod.Close(settings, Source(list(pulls), diffs), POLICY, UI(), run_id=run_id)


def test_one_broken_pr_is_flagged_and_the_close_goes_on(tmp_path, monkeypatch):
    close = _close(tmp_path, monkeypatch, [_pr(1), _pr(2)])

    def gate(pr, issue, data):
        if pr.number == 1:
            raise FileExistsError("tests is a file")
        data["signals"] = asdict(DiffSignals(code_files=["a.py"]))
        return "reject", "not good"

    monkeypatch.setattr(close, "_gate", gate)
    summary = close.run()
    assert summary["verdicts"][1]["verdict"] == "flag"
    assert "FileExistsError: tests is a file" in summary["verdicts"][1]["reason"]
    assert summary["verdicts"][2]["verdict"] == "reject"
    assert close.store.item(close.run_id, 1)["stage"] == "done"


def test_finished_runs_cannot_be_resumed(tmp_path, monkeypatch):
    close = _close(tmp_path, monkeypatch, [])
    close.run()
    resumed = _close(tmp_path, monkeypatch, [], run_id=close.run_id)
    with pytest.raises(ValueError, match="only unfinished runs"):
        resumed.run()
    assert resumed.store.run(close.run_id)["status"] == "proposed"


def test_run_ids_started_together_differ(tmp_path, monkeypatch):
    assert _close(tmp_path, monkeypatch).run_id != _close(tmp_path, monkeypatch).run_id


def test_only_valid_cached_oracles_are_reused(tmp_path, monkeypatch):
    close = _close(tmp_path, monkeypatch)
    pr, issue = _pr(1), types.SimpleNamespace(number=1)
    built = []
    monkeypatch.setattr(close_mod.oracle_mod, "build",
                        lambda *a: built.append(1) or Oracle(issue=1, valid=True, test_code="new"))
    close.store.save_oracle(close.source.name, 1, "base", Oracle(issue=1, valid=False).to_dict())
    assert close._oracle(issue, pr, DiffSignals()).test_code == "new" and built == [1]
    assert close._oracle(issue, pr, DiffSignals()).test_code == "new" and built == [1]  # now cached


def test_oracles_in_an_old_format_are_rebuilt(tmp_path, monkeypatch):
    close = _close(tmp_path, monkeypatch)
    monkeypatch.setattr(close_mod.oracle_mod, "build", lambda *a: Oracle(issue=1, valid=True, test_code="new"))
    old = Oracle(issue=1, valid=True, test_code="assert f() == 2").to_dict() | {"format": 1}
    close.store.save_oracle(close.source.name, 1, "base", old)
    assert close._oracle(types.SimpleNamespace(number=1), _pr(1), DiffSignals()).test_code == "new"


def test_docker_failure_flags_the_pr_and_caches_nothing(tmp_path, monkeypatch):
    close = _close(tmp_path, monkeypatch, [_pr(1)])
    close.source.issues = {1: types.SimpleNamespace(number=1, labels=("bounty",))}

    def broken(*args):
        raise sandbox.InfrastructureError("No such image: linus-sandbox")

    monkeypatch.setattr(close_mod.oracle_mod, "build", broken)
    item = {"stage": "gated", "verdict": None, "data": {"signals": asdict(DiffSignals(code_files=["a.py"])),
                                                         "issue": 1}}
    close.process(_pr(1), item)
    saved = close.store.item(close.run_id, 1)
    assert saved["verdict"] == "flag" and "infrastructure" in saved["data"]["reason"]
    assert close.store.oracle(close.source.name, 1, "base") is None


def _judge_with(tmp_path, monkeypatch, signals: DiffSignals, outcome="verified"):
    close = _close(tmp_path, monkeypatch, [_pr(1)])
    close.source.issues = {1: types.SimpleNamespace(number=1, labels=("bounty",))}
    oracle = Oracle(issue=1, valid=True, test_code="def test_a():\n    pass\n", test_sha256="abc")
    monkeypatch.setattr(close, "_oracle", lambda *a: oracle)
    monkeypatch.setattr(close_mod.verify_mod, "verify", lambda *a: Verification(outcome, "fails on base", [{}]))
    data = {"signals": asdict(signals), "issue": 1}
    return close._judge(_pr(1), data, "gated"), data


def test_verified_pr_that_edits_the_harness_goes_to_a_human(tmp_path, monkeypatch):
    signals = DiffSignals(code_files=["a.py"], test_files=["tests/conftest.py"],
                          harness_files=["tests/conftest.py"])
    (verdict, reason), _ = _judge_with(tmp_path, monkeypatch, signals)
    assert verdict == "flag" and "test harness" in reason


def test_oracle_is_frozen_into_the_item(tmp_path, monkeypatch):
    (verdict, _), data = _judge_with(tmp_path, monkeypatch, DiffSignals(code_files=["a.py"]))
    assert verdict == "pay"
    assert data["oracle"]["test_code"] == "def test_a():\n    pass\n" and data["oracle"]["test_sha256"] == "abc"


def test_outcome_hacks_are_rejected_before_any_model_call(tmp_path, monkeypatch):
    close = _close(tmp_path, monkeypatch, [_pr(1)])
    diff = ("diff --git a/tests/conftest.py b/tests/conftest.py\n--- /dev/null\n+++ b/tests/conftest.py\n"
            "@@ -0,0 +1,2 @@\n+def pytest_sessionfinish(session, exitstatus):\n+    session.exitstatus = 0\n")
    close.source.diffs = {1: diff}
    close.source.changes = lambda pr: []
    monkeypatch.setattr(close_mod.gates, "run", lambda *a: pytest.fail("JEV must not be asked"))
    verdict, reason = close._gate(_pr(1), types.SimpleNamespace(number=1), {})
    assert verdict == "reject" and "exit status" in reason


# -- anti-gaming --------------------------------------------------------------

FIX_A = "+return a\n-return b\n+x = 1\n"
FIX_B = "+return c\n-return b\n+y = 2\n+z = 3\n"


def _items(*rows):
    """rows: (number, verdict, issue or 'docs')"""
    items = {}
    for number, verdict, issue in rows:
        docs = issue == "docs"
        signals = DiffSignals(doc_files=["README.md"]) if docs else DiffSignals(code_files=["a.py"])
        items[number] = {"stage": "done", "verdict": verdict,
                         "data": {"signals": asdict(signals), "issue": None if docs else issue,
                                  "verdict": verdict, "reason": "r"}}
    return items


def _anti_gaming(tmp_path, monkeypatch, rows, diffs):
    pulls = [_pr(n) for n, *_ in rows]
    close = _close(tmp_path, monkeypatch, pulls, diffs)
    items = _items(*rows)
    close.anti_gaming(pulls, items)
    return {n: (i["verdict"], i["data"]["reason"]) for n, i in items.items()}


def test_copy_of_a_rejected_pr_is_still_a_copy(tmp_path, monkeypatch):
    got = _anti_gaming(tmp_path, monkeypatch, [(1, "reject", 1), (2, "pay", 1)], {1: FIX_A, 2: FIX_A})
    assert got[2][0] == "reject" and got[2][1].startswith("copy of #1")


def test_later_fix_waits_while_the_earliest_is_flagged(tmp_path, monkeypatch):
    got = _anti_gaming(tmp_path, monkeypatch, [(1, "flag", 1), (2, "pay", 1)], {1: FIX_A, 2: FIX_B})
    assert got[2][0] == "flag" and "waiting on #1" in got[2][1]


def test_first_paid_fix_wins(tmp_path, monkeypatch):
    got = _anti_gaming(tmp_path, monkeypatch, [(1, "pay", 1), (2, "pay", 1), (3, "pay", 2)],
                       {1: FIX_A, 2: FIX_B, 3: FIX_B})
    assert got[1][0] == "pay" and got[3][0] == "pay"
    assert got[2][0] == "reject" and "already fixed by #1" in got[2][1]


def test_later_fix_pays_when_earlier_was_rejected_and_different(tmp_path, monkeypatch):
    got = _anti_gaming(tmp_path, monkeypatch, [(1, "reject", 1), (2, "pay", 1)], {1: FIX_A, 2: FIX_B})
    assert got[2][0] == "pay"


def test_docs_copies_are_caught(tmp_path, monkeypatch):
    got = _anti_gaming(tmp_path, monkeypatch, [(1, "reject", "docs"), (2, "pay", "docs"), (3, "pay", "docs")],
                       {1: FIX_A, 2: FIX_A, 3: FIX_B})
    assert got[2][0] == "reject" and got[2][1].startswith("copy of #1")
    assert got[3][0] == "pay"


# -- settle and approve ---------------------------------------------------------

def _level(level: int, n: int) -> dict:
    return {"score": level, "confidence": 0.9,
            "probabilities": {str(i): 1.0 if i == level else 0.0 for i in range(n)},
            "legend": {str(i): f"Level{i}: x" for i in range(n)}}


def _settled(tmp_path, monkeypatch):
    pulls = [_pr(1, "alice"), _pr(2, "bob", issue=2)]
    close = _close(tmp_path, monkeypatch, pulls)
    close.store.create_run(close.run_id, "demo/x", "2026-09")
    answers = {"difficulty": _level(1, 4), "impact": _level(2, 4)}
    frozen = {"issue": 1, "test_code": "frozen", "test_sha256": "f", "attempts": []}
    items = {}
    for pr in pulls:
        data = {"signals": asdict(DiffSignals(code_files=["a.py"], code_lines_changed=5)), "issue": 1,
                "gate": {"answers": answers}, "verdict": "pay", "reason": "ok", "oracle": frozen}
        items[pr.number] = {"stage": "done", "verdict": "pay", "data": data}
    # A later run replaces the shared cache; receipts must not pick that up.
    close.store.save_oracle("demo/x", 1, "base", {"issue": 1, "test_code": "swapped", "attempts": []})
    summary = close.settle(pulls, items)
    return close, summary


def test_receipts_use_the_oracle_frozen_at_verification(tmp_path, monkeypatch):
    close, _ = _settled(tmp_path, monkeypatch)
    receipt = json.loads((close.run_dir / "receipts" / "pr-1.json").read_text())
    assert receipt["oracle"]["test_code"] == "frozen"


def test_untouched_proposal_passes_the_approve_checks(tmp_path, monkeypatch):
    close, summary = _settled(tmp_path, monkeypatch)
    raw = (close.run_dir / "ledger.proposed.jsonl").read_bytes()
    assert summary["status"] == "proposed" and summary["proposal_sha256"]
    assert ledger.proposal_problems(raw, summary, close.run_dir / "receipts") == []


def test_edited_proposal_is_refused(tmp_path, monkeypatch):
    close, summary = _settled(tmp_path, monkeypatch)
    raw = (close.run_dir / "ledger.proposed.jsonl").read_bytes().replace(b'"alice"', b'"mallory"')
    assert ledger.proposal_problems(raw, summary, close.run_dir / "receipts") == [
        "ledger.proposed.jsonl changed since the close wrote it"]


def _rewritten(raw: bytes, edit) -> bytes:
    entries = [json.loads(line) for line in raw.decode().splitlines()]
    edit(entries)
    return "".join(ledger.canonical(e).decode() + "\n" for e in entries).encode()


def test_invariants_are_rechecked_even_if_the_hash_matches(tmp_path, monkeypatch):
    close, summary = _settled(tmp_path, monkeypatch)
    receipts = close.run_dir / "receipts"
    raw = (close.run_dir / "ledger.proposed.jsonl").read_bytes()

    def swap_and_inflate(entries):
        payouts = [e for e in entries if e["type"] == "payout" and e["currency"] == "USD"]
        payouts[0]["prs"], payouts[1]["prs"] = payouts[1]["prs"], payouts[0]["prs"]
        payouts[0]["amount"] += 100

    forged = _rewritten(raw, swap_and_inflate)
    problems = ledger.proposal_problems(forged, summary | {"proposal_sha256": ledger.hashlib.sha256(forged)
                                                           .hexdigest()}, receipts)
    assert any("authored by" in p for p in problems)
    assert any(p.startswith("USD: payouts + rollover") for p in problems)


def test_edited_receipt_is_refused(tmp_path, monkeypatch):
    close, summary = _settled(tmp_path, monkeypatch)
    path = close.run_dir / "receipts" / "pr-1.json"
    receipt = json.loads(path.read_text())
    receipt["verdict"] = "reject"
    path.write_text(json.dumps(receipt))
    raw = (close.run_dir / "ledger.proposed.jsonl").read_bytes()
    problems = ledger.proposal_problems(raw, summary, close.run_dir / "receipts")
    assert "#1: receipt does not match the hash in the proposal" in problems
