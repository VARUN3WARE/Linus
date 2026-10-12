"""Where PRs come from. LocalSource reads a bounty month stored on disk: a git
repo with one branch per PR, plus fixture.json with what GitHub would report
(issues, PR titles, bodies, authors, dates). See demo.py for the format.
GitHubSource reads a real repository, read-only: metadata from the REST API,
code from a local clone that also fetches every PR's head.
"""

import base64
import json
import os
import re
import subprocess
import tarfile
import io
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from linus.config import Settings
from linus.github import GitHub, split_repo

LINKED_ISSUE = re.compile(r"(?i)\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s*:?\s+#(\d+)")


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    author: str
    created_at: str
    base_sha: str
    head_sha: str
    issue_numbers: tuple[int, ...]
    branch: str = ""


@dataclass(frozen=True)
class FileChange:
    path: str
    status: str  # A, M, D or R
    added: int
    removed: int


def linked_issues(text: str) -> tuple[int, ...]:
    return tuple(dict.fromkeys(int(n) for n in LINKED_ISSUE.findall(text)))


class LocalSource:
    kind = "local"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        fixture = json.loads((root / "fixture.json").read_text())
        self.name = fixture["repo"]
        self.period = fixture["period"]
        self.issues = {
            i["number"]: Issue(i["number"], i["title"], i["body"], tuple(i.get("labels", ())))
            for i in fixture["issues"]
        }
        self._pulls = fixture["pulls"]

    def _git(self, *args: str) -> str:
        out = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True)
        return out.stdout

    def pulls(self) -> list[PullRequest]:
        result = []
        for p in sorted(self._pulls, key=lambda p: (p["created_at"], p["number"])):
            head = self._git("rev-parse", p["branch"]).strip()
            base = self._git("merge-base", p["base"], p["branch"]).strip()
            result.append(PullRequest(
                number=p["number"], title=p["title"], body=p["body"], author=p["author"],
                created_at=p["created_at"], base_sha=base, head_sha=head,
                issue_numbers=linked_issues(f"{p['title']}\n{p['body']}"), branch=p["branch"],
            ))
        return result

    def diff(self, pr: PullRequest) -> str:
        return self._git("diff", "--no-color", "--no-renames", "--unified=3", pr.base_sha, pr.head_sha)

    def changes(self, pr: PullRequest) -> list[FileChange]:
        """Renames show as a delete plus an add, so both paths are attributed."""
        statuses = {}
        for line in self._git("diff", "--no-renames", "--name-status", pr.base_sha, pr.head_sha).splitlines():
            parts = line.split("\t")
            statuses[parts[-1]] = parts[0][0]
        changes = []
        for line in self._git("diff", "--no-renames", "--numstat", pr.base_sha, pr.head_sha).splitlines():
            added, removed, path = line.split("\t", 2)
            changes.append(FileChange(path, statuses.get(path, "M"),
                                      int(added) if added != "-" else 0,
                                      int(removed) if removed != "-" else 0))
        return changes

    def files_at(self, sha: str) -> list[str]:
        return self._git("ls-tree", "-r", "--name-only", sha).splitlines()

    def read(self, sha: str, path: str) -> str | None:
        try:
            return self._git("show", f"{sha}:{path}")
        except subprocess.CalledProcessError:
            return None

    def export(self, sha: str, dest: Path) -> None:
        """Write the tree at `sha` into `dest` (no .git directory)."""
        archive = subprocess.run(["git", "archive", "--format=tar", sha], cwd=self.repo,
                                 capture_output=True, check=True).stdout
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(dest, filter="data")


class GitHubSource(LocalSource):
    """One month of a GitHub repo's PRs. It only reads: nothing here writes to GitHub."""

    kind = "github"

    def __init__(self, repo: str, settings: Settings, period: str, contributor_from_commits: bool = False,
                 issue_labels: tuple[str, ...] = ("bounty",), client: GitHub | None = None,
                 remote: str | None = None) -> None:
        owner, name = split_repo(repo)
        self.name, self.period = f"{owner}/{name}", period
        self.contributor_from_commits = contributor_from_commits
        self.issue_labels = issue_labels
        self.client = client or GitHub(settings.github_token)
        self.token = settings.github_token
        self.remote = remote or f"https://github.com/{self.name}.git"
        self.root = settings.state_dir / "github"
        self.repo = self.root / f"{owner}__{name}"
        self._synced = False

    @cached_property
    def issues(self) -> dict[int, Issue]:
        issues = {}
        for label in self.issue_labels:
            for i in self.client.paginate(f"/repos/{self.name}/issues", labels=label, state="all"):
                if "pull_request" in i:
                    continue
                issues[i["number"]] = Issue(i["number"], i["title"], i.get("body") or "",
                                            tuple(lb["name"] for lb in i.get("labels", ())))
        return issues

    def _sync(self) -> None:
        """Clone (or fetch) the repo, with every PR's head as refs/remotes/pull/<n>."""
        if self._synced:
            return
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        if self.token and self.remote.startswith("https://github.com/"):
            # In the environment, not argv, so the token never shows up in a process list.
            basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env |= {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"}
        if not (self.repo / ".git").exists():
            self.root.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "--quiet", "--no-checkout", self.remote, str(self.repo)],
                           capture_output=True, text=True, check=True, env=env)
        subprocess.run(["git", "fetch", "--quiet", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*",
                        "+refs/pull/*/head:refs/remotes/pull/*"],
                       cwd=self.repo, capture_output=True, text=True, check=True, env=env)
        self._synced = True

    def pulls(self) -> list[PullRequest]:
        listed = self.client.paginate(f"/repos/{self.name}/pulls", state="all", sort="created", direction="asc")
        month = [p for p in listed if p["created_at"].startswith(self.period)]
        self._sync()
        result = []
        for p in sorted(month, key=lambda p: (p["created_at"], p["number"])):
            head = p["head"]["sha"]
            try:
                base = self._git("merge-base", f"origin/{p['base']['ref']}", head).strip()
            except subprocess.CalledProcessError:  # base branch deleted since: use the one GitHub recorded
                base = p["base"]["sha"]
            author = p["user"]["login"]
            if self.contributor_from_commits:
                author = self._git("log", "-1", "--format=%an", head).strip() or author
            body = p.get("body") or ""
            result.append(PullRequest(
                number=p["number"], title=p["title"], body=body, author=author,
                created_at=p["created_at"], base_sha=base, head_sha=head,
                issue_numbers=linked_issues(f"{p['title']}\n{body}"), branch=p["head"]["ref"],
            ))
        return result
