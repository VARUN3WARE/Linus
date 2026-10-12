"""`linus demo --publish OWNER/REPO`: puts the demo month on GitHub as a real
public repo, so anyone can watch Linus judge real PRs, or open one and try to
fool it.

Idempotent: the repo, labels, issues (matched by title) and PRs (matched by
head branch) that already exist are left alone. Branches are pushed with git
and the machine's own git credentials; everything else goes through the API.
"""

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from linus import demo
from linus.github import BOUNTY_LABEL, VERDICT_LABELS, GitHub, GitHubError, split_repo
from linus.source import LINKED_ISSUE

DESCRIPTION = "Live demo for Linus: a bounty month with real PRs and public verdicts."


class PublishError(Exception):
    pass


def _git(repo_dir: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=repo_dir, capture_output=True, text=True,
                         env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    if out.returncode != 0:
        raise PublishError(f"git {args[0]} failed: {(out.stderr or out.stdout).strip()[:300]}")
    return out.stdout.strip()


def _ensure_repo(gh: GitHub, repo: str, log: Callable[[str], None]) -> None:
    owner, name = split_repo(repo)
    if gh.exists(f"/repos/{repo}"):
        log(f"repo {repo} exists")
        return
    if gh.login().lower() != owner.lower():
        raise PublishError(f"{repo} doesn't exist and can only be created under your own account "
                           f"({gh.login()})")
    gh.post("/user/repos", {"name": name, "description": DESCRIPTION, "private": False, "auto_init": False,
                            "has_issues": True})
    log(f"created public repo {repo}")


def _push(repo_dir: Path, remote: str, repo: str, branches: list[str], force: bool,
          log: Callable[[str], None]) -> None:
    main = demo.readme_commit(repo_dir, repo)
    listed = _git(repo_dir, "ls-remote", remote, "refs/heads/main")
    remote_main = listed.split()[0] if listed else None
    if remote_main and remote_main != main and not force:
        raise PublishError(f"{repo} already has a main branch that differs from the demo's "
                           f"({remote_main[:12]} vs {main[:12]}); pass --force to overwrite it")
    flags = ["--force"] if force or remote_main is None else []
    # main goes first and alone, so an empty repo takes it as its default branch.
    _git(repo_dir, "push", "--quiet", *flags, remote, f"{main}:refs/heads/main")
    _git(repo_dir, "push", "--quiet", *flags, remote, *[f"refs/heads/{b}:refs/heads/{b}" for b in branches])
    log(f"pushed main ({main[:12]}, demo README) and {len(branches)} PR branches"
        + (" (forced)" if flags else ""))


def _issues(gh: GitHub, repo: str, issues: list[dict], log: Callable[[str], None]) -> dict[int, int]:
    """Create the bounty issues in order. Returns demo issue number -> GitHub number."""
    existing = {i["title"]: i["number"] for i in gh.paginate(f"/repos/{repo}/issues", state="all")
                if "pull_request" not in i}
    numbers = {}
    for issue in sorted(issues, key=lambda i: i["number"]):
        if issue["title"] not in existing:
            created = gh.post(f"/repos/{repo}/issues", {"title": issue["title"], "body": issue["body"],
                                                        "labels": issue["labels"]})
            existing[issue["title"]] = created["number"]
            log(f"created issue #{created['number']} {issue['title']}")
        numbers[issue["number"]] = existing[issue["title"]]
    if moved := {d: g for d, g in numbers.items() if d != g}:
        log(f"issues got different numbers than in the demo ({moved}); PR links are rewritten to match")
    return numbers


def relink(body: str, numbers: dict[int, int]) -> str:
    """Point "Fixes #N" at the issue's number on GitHub."""
    def swap(m):
        n = int(m.group(1))
        return m.group(0)[: m.start(1) - m.start(0)] + str(numbers.get(n, n))

    return LINKED_ISSUE.sub(swap, body)


def _pulls(gh: GitHub, repo: str, pulls: list[dict], numbers: dict[int, int], log: Callable[[str], None]) -> None:
    existing = {p["head"]["ref"] for p in gh.paginate(f"/repos/{repo}/pulls", state="all")}
    for pr in sorted(pulls, key=lambda p: (p["created_at"], p["number"])):
        if pr["branch"] in existing:
            continue
        body = f"{relink(pr['body'], numbers)}\n\nDemo contributor: {pr['author']}"
        created = gh.post(f"/repos/{repo}/pulls", {"title": pr["title"], "head": pr["branch"],
                                                   "base": pr["base"], "body": body})
        log(f"opened PR #{created['number']} {pr['title']} ({pr['author']})")


def publish(root: Path, repo: str, gh: GitHub, force: bool = False, remote: str | None = None,
            log: Callable[[str], None] = print) -> None:
    """Publish the demo month built under `root` (see demo.build) as `repo`."""
    split_repo(repo)
    fixture = json.loads((root / "fixture.json").read_text())
    remote = remote or f"https://github.com/{repo}.git"
    _ensure_repo(gh, repo, log)
    _push(root / "repo", remote, repo, [p["branch"] for p in fixture["pulls"]], force, log)
    try:
        if gh.get(f"/repos/{repo}").get("default_branch") != "main":
            gh.patch(f"/repos/{repo}", {"default_branch": "main"})
    except GitHubError as exc:
        log(f"could not set the default branch to main: {exc}")
    if created := gh.ensure_labels(repo, [BOUNTY_LABEL, *VERDICT_LABELS.values()]):
        log(f"created labels: {', '.join(created)}")
    numbers = _issues(gh, repo, fixture["issues"], log)
    _pulls(gh, repo, fixture["pulls"], numbers, log)
