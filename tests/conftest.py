"""An in-memory GitHub REST API for httpx.MockTransport, so GitHub tests never
touch the network."""

import json
import re
from urllib.parse import unquote

import httpx
import pytest

from linus.github import GitHub


class FakeGitHub:
    """Just enough of the REST API for publish, GitHubSource and the report."""

    def __init__(self, login: str = "maintainer") -> None:
        self.login = login
        self.repos: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.fail: re.Pattern | None = None  # paths matching this answer 502

    def add_repo(self, full: str) -> dict:
        repo = {"full_name": full, "default_branch": "main", "labels": [], "issues": [], "comments": [],
                "next": 1}
        self.repos[full] = repo
        return repo

    def client(self) -> GitHub:
        return GitHub("tok", transport=httpx.MockTransport(self.handle), sleep=lambda s: None)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = unquote(request.url.path), request.method
        if self.fail and self.fail.search(path):
            return httpx.Response(502, json={"message": "bad gateway"})
        body = json.loads(request.content) if request.content else {}
        q = dict(request.url.params)
        if path == "/user":
            return httpx.Response(200, json={"login": self.login})
        if path == "/user/repos" and method == "POST":
            repo = self.add_repo(f"{self.login}/{body['name']}")
            repo["created"] = body
            return httpx.Response(201, json={"full_name": repo["full_name"]})
        m = re.match(r"^/repos/([^/]+/[^/]+)(/.*)?$", path)
        if not m or m.group(1) not in self.repos:
            return httpx.Response(404, json={"message": "Not Found"})
        repo, rest = self.repos[m.group(1)], m.group(2) or ""
        return self._route(repo, method, rest, body, q)

    def _issue(self, repo: dict, number: int) -> dict:
        return next(i for i in repo["issues"] if i["number"] == number)

    def _route(self, repo, method, rest, body, q) -> httpx.Response:
        ok = lambda data, status=200: httpx.Response(status, json=data)  # noqa: E731
        if rest == "":
            if method == "PATCH":
                repo.update(body)
            return ok({"full_name": repo["full_name"], "default_branch": repo["default_branch"]})
        if rest == "/labels":
            if method == "POST":
                repo["labels"].append(body)
                return ok(body, 201)
            return ok(repo["labels"])
        if rest in ("/issues", "/pulls"):
            if method == "POST":
                item = {"number": repo["next"], "title": body["title"], "body": body.get("body"),
                        "user": {"login": self.login}, "labels": [{"name": n} for n in body.get("labels", [])],
                        "html_url": f"https://github.com/{repo['full_name']}/issues/{repo['next']}",
                        "created_at": "2026-10-12T10:00:00Z"}
                if rest == "/pulls":
                    item |= {"pull_request": {}, "head": {"ref": body["head"], "sha": body.get("sha", "")},
                             "base": {"ref": body["base"], "sha": ""}}
                repo["next"] += 1
                repo["issues"].append(item)
                return ok(item, 201)
            items = repo["issues"]
            if rest == "/pulls":
                items = [i for i in items if "pull_request" in i]
            if "labels" in q:
                items = [i for i in items if q["labels"] in {lb["name"] for lb in i["labels"]}]
            if "creator" in q:
                items = [i for i in items if i["user"]["login"] == q["creator"]]
            return ok(items)
        if m := re.match(r"^/issues/(\d+)$", rest):
            issue = self._issue(repo, int(m.group(1)))
            issue.update(body)
            return ok(issue)
        if m := re.match(r"^/issues/(\d+)/comments$", rest):
            n = int(m.group(1))
            if method == "POST":
                comment = {"id": len(repo["comments"]) + 1, "issue": n, "body": body["body"],
                           "user": {"login": self.login}}
                repo["comments"].append(comment)
                return ok(comment, 201)
            return ok([c for c in repo["comments"] if c["issue"] == n])
        if m := re.match(r"^/issues/comments/(\d+)$", rest):
            comment = repo["comments"][int(m.group(1)) - 1]
            comment["body"] = body["body"]
            return ok(comment)
        if m := re.match(r"^/issues/(\d+)/labels(?:/(.+))?$", rest):
            issue = self._issue(repo, int(m.group(1)))
            if method == "POST":
                issue["labels"] += [{"name": n} for n in body["labels"]]
            elif method == "DELETE":
                issue["labels"] = [lb for lb in issue["labels"] if lb["name"] != m.group(2)]
            return ok(issue["labels"])
        return httpx.Response(404, json={"message": f"no route {method} {rest}"})

    def comments(self, full: str, number: int) -> list[str]:
        return [c["body"] for c in self.repos[full]["comments"] if c["issue"] == number]

    def labels_of(self, full: str, number: int) -> set[str]:
        return {lb["name"] for lb in self._issue(self.repos[full], number)["labels"]}


@pytest.fixture
def fake_github() -> FakeGitHub:
    return FakeGitHub()
