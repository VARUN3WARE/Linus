"""Where PRs come from. LocalSource reads a bounty month stored on disk: a git
repo with one branch per PR, plus fixture.json with what GitHub would report
(issues, PR titles, bodies, authors, dates). See demo.py for the format.
"""

import json
import re
import subprocess
import tarfile
import io
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class FileChange:
    path: str
    status: str  # A, M, D or R
    added: int
    removed: int


def linked_issues(text: str) -> tuple[int, ...]:
    return tuple(dict.fromkeys(int(n) for n in LINKED_ISSUE.findall(text)))


class LocalSource:
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
                issue_numbers=linked_issues(f"{p['title']}\n{p['body']}"),
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
