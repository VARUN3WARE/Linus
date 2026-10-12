"""Running Linus on your own repos: rules, dependencies, setup, status, payouts."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from linus import cli, diffcheck, manage, policy, repos, sandbox
from linus.ledger import append, read
from linus.source import FileChange

ROOT = Path(__file__).resolve().parents[1]


# -- rules ----------------------------------------------------------------------

def test_starter_rules_parse_with_the_given_pool():
    rules = policy.parse(policy.starter(750, "EUR").encode(), "here")
    assert rules.budget == {"EUR": 750.0}
    assert rules.issue_labels == ("bounty",)
    assert rules.origin == "here"


@pytest.mark.parametrize("text, words", [
    (b"just text", "expected sections"),
    (b"budget: {}\nscoring: {}\ncaps: {}", "at least one currency"),
    (b"budget: {USD: 100}\nscoring: {base: 20, difficulty: [1, 2], impact: [1, 1, 1, 1], docs: [0, 1, 2]}\n"
     b"caps: {per_contributor_pct: 30}", "scoring.difficulty must list 4"),
    (b"budget: {USD: 100}\nscoring: {base: 20, difficulty: [0, 1, 2, 3], impact: [1, 1, 1, 1], docs: [0, 1, 2]}\n"
     b"caps: {per_contributor_pct: 300}", "between 1 and 100"),
    (b"budget: {USD: lots}\nscoring: {base: 20, difficulty: [0, 1, 2, 3], impact: [1, 1, 1, 1], docs: [0, 1, 2]}\n"
     b"caps: {per_contributor_pct: 30}", "budget.USD must be a number"),
])
def test_bad_rules_get_a_plain_error(text, words):
    with pytest.raises(policy.PolicyError, match=words):
        policy.parse(text)


def test_shipped_rules_still_load():
    rules = policy.load(ROOT / "bounty.yml")
    assert rules.primary_currency == "USD"


def test_rules_come_from_the_repo_first(fake_github, tmp_path):
    repo = fake_github.add_repo("acme/widgets")
    gh = fake_github.client()
    default = ROOT / "bounty.yml"

    rules, notes = repos.rules("acme/widgets", tmp_path, gh, default=default)
    assert rules.origin == str(default) and any("no .github/bounty.yml" in n for n in notes)

    local = repos.home(tmp_path, "acme/widgets") / "bounty.yml"
    local.parent.mkdir(parents=True)
    local.write_text(policy.starter(300))
    rules, _ = repos.rules("acme/widgets", tmp_path, gh, default=default)
    assert rules.budget == {"USD": 300.0}

    repo["files"][".github/bounty.yml"] = policy.starter(900)
    rules, notes = repos.rules("acme/widgets", tmp_path, gh, default=default)
    assert rules.budget == {"USD": 900.0} and rules.origin == "acme/widgets:.github/bounty.yml" and not notes

    override = tmp_path / "other.yml"
    override.write_text(policy.starter(5))
    assert repos.rules("acme/widgets", tmp_path, gh, override)[0].budget == {"USD": 5.0}


def test_broken_rules_in_the_repo_stop_the_close(fake_github, tmp_path):
    fake_github.add_repo("acme/widgets")["files"][".github/bounty.yml"] = "budget: nope"
    with pytest.raises(policy.PolicyError, match="acme/widgets:.github/bounty.yml"):
        repos.rules("acme/widgets", tmp_path, fake_github.client())


# -- dependencies ---------------------------------------------------------------

def test_dependencies_from_requirements_and_pyproject():
    files = {
        "requirements.txt": "requests>=2  # http\n\n-r extra.txt\n-e .\nnumpy==2.1\n",
        "requirements-dev.txt": "pytest-mock\n",
        "pyproject.toml": """
[project]
name = "widgets"
dependencies = ["attrs>=23", "requests>=2"]
[project.optional-dependencies]
test = ["hypothesis", "widgets[extra]"]
docs = ["sphinx"]
[dependency-groups]
dev = ["freezegun", {include-group = "test"}]
""",
    }
    reqs, notes = repos.dependencies(files)
    assert reqs == ["pytest-mock", "requests>=2", "numpy==2.1", "attrs>=23", "hypothesis", "freezegun"]
    assert any("-r extra.txt" in n for n in notes)


def test_poetry_and_setup_py_are_explained_not_run():
    _, notes = repos.dependencies({"pyproject.toml": "[tool.poetry]\nname = 'x'\n", "setup.py": "import os"})
    assert any("Poetry" in n for n in notes) and any("setup.py" in n for n in notes)


def test_dependency_changes_are_flagged_and_not_docs():
    changes = [FileChange("requirements.txt", "M", 1, 0)]
    diff = "diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n+++ b/requirements.txt\n" \
           "@@ -1 +1,2 @@\n requests\n+left-pad\n"
    signals = diffcheck.analyze(diff, changes, "Add a dependency")
    assert signals.dependency_files == ["requirements.txt"]
    assert not signals.docs_only and signals.changes_dependencies
    assert not diffcheck.analyze("", [FileChange("docs/guide.md", "M", 1, 0)], "").changes_dependencies


def test_repo_image_names_are_valid_docker_tags():
    assert sandbox.repo_image("Acme-Co/My.Widgets_2") == "linus-sandbox-acme-co-my-widgets-2"


def test_base_image_without_a_repo(monkeypatch):
    calls = []
    monkeypatch.setattr(sandbox.subprocess, "run", lambda *a, **k: calls.append(a))
    assert sandbox.use_repo_image(None) == sandbox.BASE_IMAGE and not calls


# -- linus init -----------------------------------------------------------------

def _cli_env(monkeypatch, tmp_path, fake_github):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LINUS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(cli, "GitHub", lambda token: fake_github.client())


def test_init_sets_a_repo_up(monkeypatch, tmp_path, fake_github):
    repo = fake_github.add_repo("acme/widgets")
    repo["files"] |= {"widgets/core.py": "", "tests/test_core.py": "", "requirements.txt": "attrs\n"}
    _cli_env(monkeypatch, tmp_path, fake_github)
    monkeypatch.setattr(sandbox, "image_id", lambda: "img")
    built = []
    monkeypatch.setattr(repos, "build_image", lambda r, reqs, log: built.append((r, reqs)) or "linus-sandbox-x")

    result = CliRunner().invoke(cli.app, ["init", "acme/widgets", "--budget", "1200"])

    assert result.exit_code == 0, result.output
    assert {lb["name"] for lb in repo["labels"]} == {"bounty", "linus: pay", "linus: reject", "linus: flag"}
    assert built == [("acme/widgets", ["attrs"])]
    rules = policy.load(tmp_path / "state" / "repos" / "acme__widgets" / "bounty.yml")
    assert rules.budget == {"USD": 1200.0}
    record = json.loads((tmp_path / "state" / "repos" / "acme__widgets" / "repo.json").read_text())
    assert record["sandbox_image"] == "linus-sandbox-x" and record["dependencies"] == ["attrs"]
    assert "commit" in result.output and ".github/bounty.yml" in result.output


def test_init_read_only_token_skips_labels(monkeypatch, tmp_path, fake_github):
    repo = fake_github.add_repo("acme/widgets")
    repo["permissions"] = {"push": False}
    repo["files"] |= {"widgets/core.py": "", ".github/bounty.yml": policy.starter(50)}
    _cli_env(monkeypatch, tmp_path, fake_github)

    result = CliRunner().invoke(cli.app, ["init", "acme/widgets", "--no-sandbox"])

    assert result.exit_code == 0, result.output
    assert repo["labels"] == [] and "only read" in result.output
    assert not (tmp_path / "state" / "repos" / "acme__widgets" / "bounty.yml").exists()


def test_period_last_wraps_the_year(monkeypatch):
    class January(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2027, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(cli, "datetime", January)
    assert cli._period("last") == "2026-12"
    assert cli._period(None) == "2027-01"


# -- status and payouts ---------------------------------------------------------

def _run(run_id, repo, period, status, created, verdicts, payouts=None, source="github"):
    summary = {"run_dir": f".linus/runs/{run_id}", "source": source, "verdicts": verdicts,
               "allocations": [{"currency": "USD", "payouts": payouts or {}}], "problems": []}
    return {"id": run_id, "repo": repo, "period": period, "status": status, "created_at": created,
            "summary": json.dumps(summary)}


def test_status_lists_what_needs_the_maintainer():
    flagged = {"7": {"author": "bo", "verdict": "flag", "reason": "edits the test harness"},
               "8": {"author": "al", "verdict": "pay", "reason": "fixed"}}
    runs = [
        _run("r1", "acme/widgets", "2026-09", "approved", "2026-10-01T00:00", {}),
        _run("r0", "acme/widgets", "2026-09", "proposed", "2026-10-02T00:00", {}),  # later, but settled
        _run("r2", "acme/widgets", "2026-10", "proposed", "2026-11-01T00:00", flagged, {"al": 100}),
        _run("r3", "acme/gizmo", "2026-10", "running", "2026-11-01T00:00", {}),
        _run("e1", "acme/gizmo", "2026-10", "eval", "2026-11-02T00:00", {}),
    ]
    view = manage.overview(runs, [{"repo": "acme/new"}])
    assert [(r["repo"], r["period"], r["run_id"]) for r in view["rows"]] == [
        ("acme/gizmo", "2026-10", "r3"), ("acme/widgets", "2026-09", "r1"), ("acme/widgets", "2026-10", "r2")]
    todo = {(t["repo"], t["what"]) for t in view["todo"]}
    assert ("acme/widgets", "approve the payouts") in todo
    assert ("acme/widgets", "look at PR #7 by bo") in todo
    assert ("acme/gizmo", "finish the close") in todo
    assert ("acme/new", "run the first close") in todo
    url = next(t["url"] for t in view["todo"] if t["what"].startswith("look at"))
    assert url == "https://github.com/acme/widgets/pull/7"


def test_payouts_only_from_approved_runs(tmp_path):
    path = tmp_path / "ledger.jsonl"
    payout = {"type": "payout", "repo": "acme/widgets", "period": "2026-10", "currency": "USD"}
    append(path, [payout | {"run_id": "r1", "contributor": "al", "amount": 120.5, "prs": [8]},
                  {"type": "approval", "run_id": "r1", "approved_by": "=cmd()", "approved_at": "2026-11-02"},
                  payout | {"run_id": "r2", "contributor": "bo", "amount": 50, "prs": [9]}])
    rows = manage.approved_payouts(read(path), {"acme/widgets": "github"})
    assert [(r["contributor"], r["amount"]) for r in rows] == [("al", "120.50")]
    assert rows[0]["pr_links"] == "https://github.com/acme/widgets/pull/8"
    csv = manage.to_csv(rows)
    assert "'=cmd()" in csv and csv.splitlines()[0].startswith("repo,period,contributor")


def test_payouts_cli_writes_csv(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    append(state / "ledger.jsonl", [
        {"type": "payout", "repo": "acme/widgets", "period": "2026-10", "currency": "USD", "run_id": "r1",
         "contributor": "al", "amount": 10, "prs": [1]},
        {"type": "approval", "run_id": "r1", "approved_by": "me", "approved_at": "x"}])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LINUS_STATE_DIR", str(state))
    result = CliRunner().invoke(cli.app, ["payouts", "--csv", "out.csv"])
    assert result.exit_code == 0, result.output
    rows = (tmp_path / "out.csv").read_text().splitlines()
    assert len(rows) == 2 and rows[1].startswith("acme/widgets,2026-10,al,")


def test_sync_github_needs_an_approved_run(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LINUS_STATE_DIR", str(tmp_path / "state"))
    result = CliRunner().invoke(cli.app, ["approve", "nope", "--sync-github"])
    assert result.exit_code == 1 and "approve it first" in result.output
