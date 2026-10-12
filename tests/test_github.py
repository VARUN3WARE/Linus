"""The GitHub client, offline: retries, rate limits, headers and pagination."""

import time

import httpx
import pytest

from linus.github import GitHub, GitHubError, split_repo


def _client(responses, sleeps=None):
    seen = []

    def handle(request):
        seen.append(request)
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    gh = GitHub("tok", transport=httpx.MockTransport(handle), sleep=(sleeps.append if sleeps is not None
                                                                    else lambda s: None))
    return gh, seen


def test_every_request_carries_the_versioned_headers():
    gh, seen = _client([httpx.Response(200, json={"login": "me"})])
    assert gh.login() == "me"
    headers = seen[0].headers
    assert headers["accept"] == "application/vnd.github+json"
    assert headers["x-github-api-version"] == "2022-11-28"
    assert headers["authorization"] == "Bearer tok"


def test_server_errors_and_network_errors_are_retried_with_backoff():
    sleeps = []
    gh, seen = _client([httpx.Response(502), httpx.ConnectError("down"), httpx.Response(200, json={"ok": 1})],
                       sleeps)
    assert gh.get("/x") == {"ok": 1}
    assert len(seen) == 3 and sleeps == [2.0, 4.0]


def test_gives_up_after_three_attempts():
    gh, seen = _client([httpx.Response(500)] * 3)
    with pytest.raises(GitHubError) as err:
        gh.get("/x")
    assert err.value.status == 500 and len(seen) == 3


def test_rate_limits_are_waited_out():
    sleeps = []
    reset = str(int(time.time()) + 5)
    gh, _ = _client([httpx.Response(429, headers={"retry-after": "7"}),
                     httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}),
                     httpx.Response(200, json=[])], sleeps)
    assert gh.get("/x") == []
    assert sleeps[0] == 7.0 and 1 <= sleeps[1] <= 7


def test_a_plain_403_is_not_retried():
    gh, seen = _client([httpx.Response(403, json={"message": "Resource not accessible"})])
    with pytest.raises(GitHubError, match="Resource not accessible"):
        gh.post("/x", {})
    assert len(seen) == 1


def test_a_rate_limit_that_resets_too_late_fails_instead_of_blocking():
    reset = str(int(time.time()) + 3600)
    gh, seen = _client([httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset})])
    with pytest.raises(GitHubError):
        gh.get("/x")
    assert len(seen) == 1


def test_paginate_follows_next_links():
    page2 = "https://api.github.com/repos/o/r/pulls?page=2"
    gh, seen = _client([httpx.Response(200, json=[1, 2], headers={"link": f'<{page2}>; rel="next"'}),
                        httpx.Response(200, json=[3])])
    assert gh.paginate("/repos/o/r/pulls", state="all") == [1, 2, 3]
    assert seen[0].url.params["per_page"] == "100" and seen[0].url.params["state"] == "all"
    assert str(seen[1].url) == page2


def test_exists_is_false_only_on_404():
    gh, _ = _client([httpx.Response(404), httpx.Response(200, json={})])
    assert gh.exists("/repos/o/r") is False
    assert gh.exists("/repos/o/r") is True


@pytest.mark.parametrize("bad", ["", "owner", "a/b/c", "a b/c", "../x"])
def test_repo_names_are_validated(bad):
    with pytest.raises(ValueError):
        split_repo(bad)
