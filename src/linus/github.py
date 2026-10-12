"""The one place Linus talks to the GitHub REST API.

Every request carries the versioned JSON headers, is retried up to three
times on network errors and 5xx responses with exponential backoff, and waits
out rate limits (403/429 with Retry-After or an exhausted X-RateLimit-Remaining)
instead of failing. Git traffic (clone, fetch, push) goes through git itself.
"""

import re
import time
from collections.abc import Callable
from typing import Any

import httpx

API = "https://api.github.com"
ATTEMPTS = 3
BACKOFF_S = 2.0  # first retry waits this long, then it doubles
MAX_WAIT_S = 120.0  # a rate limit that resets later than this fails instead of blocking the run
SECONDARY_WAIT_S = 60.0
REPO = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")

BOUNTY_LABEL = ("bounty", "5319e7", "Paid from the monthly bounty pool")
VERDICT_LABELS = {
    "pay": ("linus: pay", "2da44e", "Linus verified this PR and proposes paying it"),
    "reject": ("linus: reject", "d73a4a", "Linus could not verify this PR"),
    "flag": ("linus: flag", "e4e669", "Linus wants a human to look at this PR"),
}


class GitHubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def split_repo(repo: str) -> tuple[str, str]:
    if not REPO.match(repo or ""):
        raise ValueError(f"{repo!r} is not a GitHub repo; expected OWNER/REPO")
    owner, name = repo.split("/")
    return owner, name


def _message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message", ""))[:200]
    except ValueError:
        return resp.text[:200]


class GitHub:
    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, base_url: str = API) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "linus"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(base_url=base_url, headers=headers, transport=transport, timeout=30)
        self._sleep = sleep
        self._login: str | None = None
        self.calls = 0

    def _retry_after(self, resp: httpx.Response, attempt: int) -> float | None:
        """Seconds to wait before retrying `resp`, or None if retrying won't help."""
        if resp.status_code >= 500:
            return BACKOFF_S * 2 ** attempt
        if resp.status_code in (403, 429):
            if after := resp.headers.get("retry-after"):
                return float(after)
            if resp.headers.get("x-ratelimit-remaining") == "0" and (reset := resp.headers.get("x-ratelimit-reset")):
                return max(float(reset) - time.time(), 0) + 1
            if resp.status_code == 429:
                return BACKOFF_S * 2 ** attempt
            if "rate limit" in _message(resp).lower():
                # GitHub's secondary (per-user) limit, often with quota left: its
                # guidance is to wait at least a minute before retrying.
                return SECONDARY_WAIT_S * 2 ** attempt
        return None  # a plain 403 is a permission problem

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(ATTEMPTS):
            last = attempt == ATTEMPTS - 1
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if last:
                    raise GitHubError(0, f"{method} {path}: {exc}") from exc
                self._sleep(BACKOFF_S * 2 ** attempt)
                continue
            self.calls += 1
            if resp.is_success:
                return resp
            wait = self._retry_after(resp, attempt)
            if wait is None or last or wait > MAX_WAIT_S:
                hint = "" if wait is None else (
                    f" (GitHub is rate-limiting these requests; try again in about {max(1, round(wait / 60))} min)")
                raise GitHubError(resp.status_code,
                                  f"{method} {path}: {resp.status_code} {_message(resp)}{hint}")
            self._sleep(wait)
        raise AssertionError("unreachable")

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None).json()

    def post(self, path: str, body: dict) -> Any:
        return self.request("POST", path, json=body).json()

    def patch(self, path: str, body: dict) -> Any:
        return self.request("PATCH", path, json=body).json()

    def delete(self, path: str) -> None:
        self.request("DELETE", path)

    def exists(self, path: str) -> bool:
        try:
            self.request("GET", path)
        except GitHubError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def paginate(self, path: str, **params: Any) -> list:
        """Every item of a list endpoint, following the Link: next headers."""
        items: list = []
        url: str | None = path
        query: dict | None = {"per_page": 100, **params}
        while url:
            resp = self.request("GET", url, params=query)
            items += resp.json()
            url = resp.links.get("next", {}).get("url")
            query = None  # the next link already carries the query
        return items

    def login(self) -> str:
        """The authenticated user, used to find Linus's own comments and issues."""
        if self._login is None:
            self._login = self.get("/user")["login"]
        return self._login

    def ensure_labels(self, repo: str, labels: list[tuple[str, str, str]]) -> list[str]:
        """Create any of (name, color, description) that `repo` doesn't have yet."""
        have = {label["name"].lower() for label in self.paginate(f"/repos/{repo}/labels")}
        created = []
        for name, color, description in labels:
            if name.lower() not in have:
                self.post(f"/repos/{repo}/labels", {"name": name, "color": color, "description": description})
                created.append(name)
        return created
