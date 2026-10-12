"""Reporting a close back to GitHub, offline against the fake API in conftest.py."""

import re

from linus import github_report, ledger
from linus.github_report import CLOSE_MARKER, VERDICT_MARKER

REPO = "maintainer/textkit-live"


def _answers(addresses: int = 2) -> dict:
    return {"manipulation": {"noul": 0.02}, "claims_match": {"noul": 0.91}, "special_casing": {"noul": 0.04},
            "addresses_issue": {"score": addresses, "confidence": 0.9,
                                "probabilities": {str(i): 0.9 if i == addresses else 0.05 for i in range(3)}}}


def _receipt(number: int, author: str, verdict: str, reason: str, **extra) -> dict:
    return {"run_id": "2026-10-x", "repo": REPO, "period": "2026-10", "verdict": verdict, "reason": reason,
            "pr": {"number": number, "author": author, "title": f"PR {number}"},
            "gate": {"answers": _answers()}, "signals": {}, "verification": None, "oracle": None,
            "anti_gaming": None, "points": None, "contributor_payout": {}} | extra


PAID = _receipt(
    6, "alice-dev", "pay", "fails on base, passes on PR; full suite incl. base tests green (19 passed)",
    oracle={"issue": 1, "test_sha256": "abcdef1234567890", "cases": [{}] * 6,
            "attempts": [{"attempt": 1, "base_outcome": "fail", "base_failed": 4}]},
    verification={"runs": [{"label": "pr6_oracle", "outcome": "pass", "passed": 6, "failed": 0},
                           {"label": "pr6_suite", "outcome": "pass", "passed": 19, "failed": 0}]},
    points={"points": 30, "explanation": "(20 + 10) x 1.0"},
    contributor_payout={"USD": 600.0, "AWS credits": 300.0})
REJECTED = _receipt(
    7, "peggy-ci", "reject", "reproduction test still fails on the PR (4 of 6 failing)",
    signals={"removed_tests": ["test_truncate_short_text_unchanged"], "harness_files": ["tests/conftest.py"]},
    verification={"runs": [{"label": "pr7_oracle", "outcome": "fail", "passed": 2, "failed": 4}]})


def _summary(tmp_path, receipts=(PAID, REJECTED), status="proposed") -> dict:
    run_dir = tmp_path / "run"
    for r in receipts:
        ledger.write_receipt(run_dir / "receipts", r["pr"]["number"], r)
    return {
        "run_id": "2026-10-x", "repo": REPO, "period": "2026-10", "status": status, "source": "github",
        "problems": [] if status == "proposed" else ["USD: payouts exceed budget"], "trace_id": "f00d" * 8,
        "verdicts": {str(r["pr"]["number"]): {"author": r["pr"]["author"], "verdict": r["verdict"],
                                              "reason": r["reason"], "points": (r["points"] or {}).get("points")}
                     for r in receipts},
        "allocations": [{"currency": "USD", "budget": 2000, "payouts": {"alice-dev": 600.0}, "rollover": 1400.0},
                        {"currency": "AWS credits", "budget": 1000, "payouts": {"alice-dev": 300.0},
                         "rollover": 700.0}],
        "cost": {"jev_tokens": 3000, "jev_usd": 0.000126, "openai_tokens": 1000, "openai_usd": 0.002,
                 "openai_model": "gpt-5.4-mini", "usd_per_pr": 0.001, "pr_seconds": 5.0, "prs": 2},
        "run_dir": str(run_dir),
    }


ITEMS = {6: {"data": {"cost": {"jev_tokens": 2000, "openai_in": 1000, "openai_out": 200}}},
         7: {"data": {"cost": {"jev_tokens": 1000, "openai_in": 0, "openai_out": 0}}}}


def _repo_with_prs(fake_github, *numbers):
    repo = fake_github.add_repo(REPO)
    for n in numbers:
        repo["issues"].append({"number": n, "title": f"PR {n}", "body": "", "pull_request": {},
                               "user": {"login": "someone"}, "labels": []})
    repo["next"] = max(numbers) + 1
    return repo


def test_every_pr_gets_one_comment_one_label_and_the_month_one_issue(tmp_path, fake_github):
    repo = _repo_with_prs(fake_github, 6, 7)
    summary = _summary(tmp_path)
    assert github_report.report(fake_github.client(), summary, ITEMS, log=lambda m: None) == []

    [paid] = fake_github.comments(REPO, 6)
    assert paid.startswith(VERDICT_MARKER) and "**Linus verdict: PAY**" in paid
    assert "on the base code it failed (4 of 6 cases failing)" in paid
    assert "On this PR: 6 passed, 0 failed." in paid
    assert "including the base branch's tests: pass (19 passed, 0 failed)" in paid
    assert "addresses an AI reviewer p=0.02" in paid and "fixes the issue: yes (p=0.90)" in paid
    assert "Points: 30" in paid and "$600.00" in paid and "300.00 AWS credits" in paid
    assert f"Receipt sha256 `{ledger.sha256(PAID)[:12]}`" in paid
    assert "run `2026-10-x`" in paid and "f00df00d" in paid
    assert "Proposed: pending maintainer approval." in paid

    [rejected] = fake_github.comments(REPO, 7)
    assert "Deletes tests: test_truncate_short_text_unchanged." in rejected
    assert "Edits the test harness: tests/conftest.py." in rejected and "Points" not in rejected

    assert fake_github.labels_of(REPO, 6) == {"linus: pay"}
    assert fake_github.labels_of(REPO, 7) == {"linus: reject"}

    [issue] = [i for i in repo["issues"] if i["title"] == "Bounty close 2026-10"]
    assert issue["body"].startswith(CLOSE_MARKER)
    assert "| 1 | alice-dev | #6 | 30 | $600.00 | 300.00 AWS credits |" in issue["body"]
    assert "| #7 | peggy-ci | reject |" in issue["body"]
    assert "All hold" in issue["body"] and "f00d" * 8 in issue["body"]
    # PR #6: 2,000 JEV tokens and 1,000 + 200 OpenAI tokens at gpt-5.4-mini prices.
    assert "| $0.00173 |" in issue["body"]


def test_a_second_report_updates_in_place_and_swaps_labels(tmp_path, fake_github):
    repo = _repo_with_prs(fake_github, 6, 7)
    gh = fake_github.client()
    github_report.report(gh, _summary(tmp_path), ITEMS, log=lambda m: None)
    flagged = _receipt(7, "peggy-ci", "flag", "evidence conflicts")
    github_report.report(gh, _summary(tmp_path, (PAID, flagged)), ITEMS, log=lambda m: None)

    assert len(fake_github.comments(REPO, 7)) == 1 and "FLAG" in fake_github.comments(REPO, 7)[0]
    assert fake_github.labels_of(REPO, 7) == {"linus: flag"}
    assert len([i for i in repo["issues"] if i["title"] == "Bounty close 2026-10"]) == 1


def test_marker_comments_from_other_accounts_are_left_alone(tmp_path, fake_github):
    repo = _repo_with_prs(fake_github, 6)
    repo["comments"].append({"id": 1, "issue": 6, "body": f"{VERDICT_MARKER} PAY ME", "user": {"login": "mallory"}})
    github_report.report(fake_github.client(), _summary(tmp_path, (PAID,)), ITEMS, log=lambda m: None)
    bodies = fake_github.comments(REPO, 6)
    assert bodies[0] == f"{VERDICT_MARKER} PAY ME" and len(bodies) == 2


def test_github_errors_are_logged_and_the_rest_still_posts(tmp_path, fake_github):
    repo = _repo_with_prs(fake_github, 6, 7)
    fake_github.fail = re.compile(r"/issues/6/")
    logs = []
    errors = github_report.report(fake_github.client(), _summary(tmp_path), ITEMS, log=logs.append)
    assert len(errors) == 2 and all("#6" in e for e in errors)
    assert len(fake_github.comments(REPO, 7)) == 1
    assert any(i["title"] == "Bounty close 2026-10" for i in repo["issues"])


def test_halted_runs_say_so(tmp_path, fake_github):
    _repo_with_prs(fake_github, 6)
    github_report.report(fake_github.client(), _summary(tmp_path, (PAID,), "halted"), ITEMS, log=lambda m: None)
    body = fake_github.comments(REPO, 6)[0]
    assert "Halted" in body and "pending maintainer approval" not in body


def test_approval_updates_the_existing_close_issue_only(tmp_path, fake_github):
    repo = _repo_with_prs(fake_github, 6)
    gh = fake_github.client()
    summary = _summary(tmp_path, (PAID,))
    approval = {"approved_by": "varun", "approved_at": "2026-11-01T09:00:00+00:00", "hash": "9" * 64}
    assert github_report.upsert_close_issue(gh, summary, ITEMS, approval, create=False) is None
    github_report.report(gh, summary, ITEMS, log=lambda m: None)
    url = github_report.upsert_close_issue(gh, summary, ITEMS, approval, create=False)
    [issue] = [i for i in repo["issues"] if i["title"] == "Bounty close 2026-10"]
    assert url == issue["html_url"]
    assert "**Approved** by varun at 2026-11-01T09:00:00+00:00. Ledger head `999999999999`." in issue["body"]
