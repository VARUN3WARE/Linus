"""Publishing the demo month and reading it back through GitHubSource, offline:
the API is a MockTransport fake and the "GitHub" git remote is a local bare repo."""

import subprocess

import pytest

from linus import demo
from linus.config import Settings
from linus.publish import DESCRIPTION, PublishError, publish, relink
from linus.source import GitHubSource

REPO = "maintainer/textkit-live"


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def month(tmp_path):
    root = demo.build(tmp_path / "month")
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    return root, bare


def _settings(tmp_path) -> Settings:
    return Settings("", "jev", "", "", "gpt-5.4-mini", "", "", "linus", "", "", "", tmp_path / "state")


def test_publish_creates_the_repo_issues_and_prs_in_order(month, fake_github):
    root, bare = month
    publish(root, REPO, fake_github.client(), remote=str(bare), log=lambda m: None)

    created = fake_github.repos[REPO]["created"]
    assert created == {"name": "textkit-live", "description": DESCRIPTION, "private": False, "auto_init": False,
                       "has_issues": True}
    assert [lb["name"] for lb in fake_github.repos[REPO]["labels"]] == [
        "bounty", "linus: pay", "linus: reject", "linus: flag"]
    items = fake_github.repos[REPO]["issues"]
    issues = [i for i in items if "pull_request" not in i]
    assert [(i["number"], i["title"]) for i in issues] == [(i["number"], i["title"]) for i in demo.ISSUES]
    assert all("bounty" in {lb["name"] for lb in i["labels"]} for i in issues)

    pulls = [i for i in items if "pull_request" in i]
    in_order = sorted(demo.PULLS, key=lambda p: p["created_at"])
    assert [p["head"]["ref"] for p in pulls] == [f"pr/{p['number']}" for p in in_order]
    assert all(p["base"]["ref"] == "main" for p in pulls)
    first = pulls[0]
    assert first["title"] == in_order[0]["title"]
    assert first["body"] == f"{in_order[0]['body']}\n\nDemo contributor: {in_order[0]['author']}"

    # main on the remote is the local main plus the demo README; the local main is untouched.
    local_main = git(root / "repo", "rev-parse", "main")
    assert git(bare, "rev-parse", "main^") == local_main
    readme = git(bare, "show", "main:README.md")
    assert "https://github.com/VARUN3WARE/Linus" in readme and "Read the verdicts" in readme
    assert "No real money is paid" in readme and "commit" in readme
    assert git(root / "repo", "show", "main:README.md").startswith("# textkit\n")
    assert git(bare, "rev-parse", "pr/101") == git(root / "repo", "rev-parse", "pr/101")


def test_publishing_twice_changes_nothing(month, fake_github):
    root, bare = month
    publish(root, REPO, fake_github.client(), remote=str(bare), log=lambda m: None)
    before = len(fake_github.requests)
    writes_before = len(fake_github.repos[REPO]["issues"])
    logs = []
    publish(root, REPO, fake_github.client(), remote=str(bare), log=logs.append)
    assert len(fake_github.repos[REPO]["issues"]) == writes_before
    new = fake_github.requests[before:]
    assert [r for r in new if r.method in ("POST", "PATCH") and "/labels" not in r.url.path] == []
    assert not any("created" in m or "opened" in m for m in logs)


def test_refuses_to_overwrite_a_different_remote_main_without_force(month, fake_github, tmp_path):
    root, bare = month
    other = tmp_path / "other"
    git(tmp_path, "init", "-q", "-b", "main", str(other))
    (other / "x.txt").write_text("someone else's repo\n")
    git(other, "add", "-A")
    git(other, "-c", "user.name=x", "-c", "user.email=x@example.com", "commit", "-q", "-m", "x")
    git(other, "push", "-q", str(bare), "main")
    fake_github.add_repo(REPO)

    with pytest.raises(PublishError, match="--force"):
        publish(root, REPO, fake_github.client(), remote=str(bare), log=lambda m: None)
    assert fake_github.repos[REPO]["issues"] == []
    publish(root, REPO, fake_github.client(), force=True, remote=str(bare), log=lambda m: None)
    assert git(bare, "rev-parse", "main^") == git(root / "repo", "rev-parse", "main")


def test_only_your_own_account_gets_a_new_repo(month, fake_github):
    root, bare = month
    with pytest.raises(PublishError, match="your own account"):
        publish(root, "someone-else/textkit", fake_github.client(), remote=str(bare), log=lambda m: None)


def test_pr_links_follow_issues_that_got_other_numbers(month, fake_github):
    root, bare = month
    repo = fake_github.add_repo(REPO)
    repo["issues"].append({"number": 1, "title": "an older issue", "body": "", "labels": [],
                           "user": {"login": "maintainer"}})
    repo["next"] = 2
    publish(root, REPO, fake_github.client(), remote=str(bare), log=lambda m: None)
    pr = next(i for i in repo["issues"] if i.get("head", {}).get("ref") == "pr/106")
    assert pr["body"].startswith("Fixes #3.")  # demo issue #2 is #3 here
    assert relink("Closes: #1 and fixes #9", {1: 4}) == "Closes: #4 and fixes #9"


# -- reading it back -------------------------------------------------------------

def _published(month, fake_github):
    """Publish, then make the bare remote and the fake API look like GitHub after the PRs were opened."""
    root, bare = month
    publish(root, REPO, fake_github.client(), remote=str(bare), log=lambda m: None)
    pulls = [i for i in fake_github.repos[REPO]["issues"] if "pull_request" in i]
    for day, pr in enumerate(pulls, 1):
        sha = git(bare, "rev-parse", pr["head"]["ref"])
        git(bare, "update-ref", f"refs/pull/{pr['number']}/head", sha)
        pr["head"]["sha"] = sha
        pr["created_at"] = f"2026-10-{day:02d}T09:00:00Z"
    pulls[-1]["created_at"] = "2026-11-01T09:00:00Z"  # next month: left out
    return root, bare, pulls


def test_github_source_reads_the_published_month(month, fake_github, tmp_path):
    root, bare, published = _published(month, fake_github)
    source = GitHubSource(REPO, _settings(tmp_path), "2026-10", contributor_from_commits=True,
                          client=fake_github.client(), remote=str(bare))
    pulls = source.pulls()

    assert [p.number for p in pulls] == [p["number"] for p in published[:-1]]
    first = pulls[0]
    in_order = sorted(demo.PULLS, key=lambda p: p["created_at"])
    assert (first.author, first.branch) == (in_order[0]["author"], f"pr/{in_order[0]['number']}")
    assert first.base_sha == git(root / "repo", "rev-parse", "main")  # the merge-base, not the README commit
    assert first.head_sha == git(root / "repo", "rev-parse", first.branch)
    assert first.issue_numbers == (1,)
    assert "range(0, len(items), size)" in source.diff(first)
    assert source.repo == tmp_path / "state" / "github" / "maintainer__textkit-live"

    assert sorted(source.issues) == [1, 2, 3, 4, 5]
    assert source.issues[2].title == demo.ISSUES[1]["title"] and "bounty" in source.issues[2].labels


def test_github_source_uses_the_account_without_demo_authors(month, fake_github, tmp_path):
    _, bare, _ = _published(month, fake_github)
    source = GitHubSource(REPO, _settings(tmp_path), "2026-10", client=fake_github.client(), remote=str(bare))
    assert {p.author for p in source.pulls()} == {"maintainer"}
    # A second source on the same clone fetches instead of cloning again.
    again = GitHubSource(REPO, _settings(tmp_path), "2026-10", client=fake_github.client(), remote=str(bare))
    assert len(again.pulls()) == len(demo.PULLS) - 1  # the last one is dated next month
