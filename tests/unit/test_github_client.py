import time

import httpx
import pytest
import respx

from reviewer.github.auth import GitHubAuthError
from reviewer.github.client import BOT_MARKER, GitHubClient, GitHubError

REPO = "octo-org/octo-repo"


@pytest.fixture
def api():
    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(sleeps):
    with GitHubClient("ghp_test", max_attempts=3, sleep=sleeps.append) as c:
        yield c


def test_missing_token_is_rejected():
    with pytest.raises(GitHubAuthError):
        GitHubClient(None)


def pr_ok(title: str = "ok") -> httpx.Response:
    return httpx.Response(200, json={"title": title, "body": None, "head": {"sha": "abc123"}})


def test_requests_send_the_token_and_github_api_headers(api, client):
    route = api.get(f"/repos/{REPO}/pulls/7").mock(return_value=pr_ok())

    client.get_pull_request(REPO, 7)

    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer ghp_test"
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["x-github-api-version"] == "2022-11-28"


def test_list_changed_files_paginates(api, client):
    page1 = [{"filename": f"f{i}.py", "status": "modified", "patch": "@@"} for i in range(100)]
    page2 = [{"filename": "last.png", "status": "added"}]
    route = api.get(f"/repos/{REPO}/pulls/7/files")
    route.side_effect = [httpx.Response(200, json=page1), httpx.Response(200, json=page2)]

    files = client.list_changed_files(REPO, 7)

    assert len(files) == 101
    assert files[-1].filename == "last.png"
    assert files[-1].patch is None
    assert route.call_count == 2


def test_get_file_content_at_sha(api, client):
    route = api.get(f"/repos/{REPO}/contents/src/app.py").respond(200, text="print('hi')")

    assert client.get_file_content(REPO, "src/app.py", "abc123") == "print('hi')"

    assert route.calls.last.request.url.params["ref"] == "abc123"
    assert route.calls.last.request.headers["accept"] == "application/vnd.github.raw+json"


def test_get_file_content_missing_file_returns_none(api, client, sleeps):
    api.get(f"/repos/{REPO}/contents/gone.py").respond(404, json={"message": "Not Found"})

    assert client.get_file_content(REPO, "gone.py", "abc123") is None
    assert sleeps == []  # a 404 is not retried


def test_upsert_creates_comment_with_marker_when_none_exists(api, client):
    api.get(f"/repos/{REPO}/issues/7/comments").respond(
        200, json=[{"id": 1, "body": "human comment"}]
    )
    post = api.post(f"/repos/{REPO}/issues/7/comments").respond(201, json={"id": 99})

    assert client.upsert_bot_comment(REPO, 7, "## Review") == 99

    assert post.calls.last.request.content.decode().count(BOT_MARKER) == 1


def test_upsert_updates_existing_bot_comment(api, client):
    api.get(f"/repos/{REPO}/issues/7/comments").respond(
        200,
        json=[
            {"id": 1, "body": "human comment"},
            {"id": 2, "body": f"{BOT_MARKER}\nold review"},
        ],
    )
    patch = api.patch(f"/repos/{REPO}/issues/comments/2").respond(200, json={"id": 2})
    post = api.post(f"/repos/{REPO}/issues/7/comments").respond(201, json={"id": 99})

    assert client.upsert_bot_comment(REPO, 7, "new review") == 2

    assert patch.call_count == 1
    assert post.call_count == 0
    assert "new review" in patch.calls.last.request.content.decode()


def test_human_comment_quoting_the_marker_is_not_hijacked(api, client):
    api.get(f"/repos/{REPO}/issues/7/comments").respond(
        200, json=[{"id": 5, "body": f"see {BOT_MARKER} above"}]
    )
    api.post(f"/repos/{REPO}/issues/7/comments").respond(201, json={"id": 99})

    assert client.upsert_bot_comment(REPO, 7, "review") == 99


def test_retries_5xx_then_succeeds(api, client, sleeps):
    route = api.get(f"/repos/{REPO}/pulls/7")
    route.side_effect = [httpx.Response(503), pr_ok()]

    assert client.get_pull_request(REPO, 7).title == "ok"

    assert route.call_count == 2
    assert len(sleeps) == 1


def test_retries_timeouts(api, client, sleeps):
    route = api.get(f"/repos/{REPO}/pulls/7")
    route.side_effect = [httpx.ConnectTimeout("timed out"), pr_ok()]

    assert client.get_pull_request(REPO, 7).title == "ok"
    assert len(sleeps) == 1


def test_gives_up_after_max_attempts(api, client, sleeps):
    route = api.get(f"/repos/{REPO}/pulls/7").respond(502)

    with pytest.raises(GitHubError) as exc_info:
        client.get_pull_request(REPO, 7)

    assert exc_info.value.status_code == 502
    assert route.call_count == 3
    assert len(sleeps) == 2


def test_persistent_network_error_becomes_github_error(api, client):
    api.get(f"/repos/{REPO}/pulls/7").mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(GitHubError):
        client.get_pull_request(REPO, 7)


def test_client_errors_are_not_retried(api, client, sleeps):
    route = api.get(f"/repos/{REPO}/pulls/7").respond(422, json={"message": "bad"})

    with pytest.raises(GitHubError) as exc_info:
        client.get_pull_request(REPO, 7)

    assert exc_info.value.status_code == 422
    assert route.call_count == 1
    assert sleeps == []


def test_plain_403_permission_error_is_not_retried(api, client, sleeps):
    route = api.get(f"/repos/{REPO}/pulls/7").respond(403, json={"message": "Forbidden"})

    with pytest.raises(GitHubError):
        client.get_pull_request(REPO, 7)

    assert route.call_count == 1
    assert sleeps == []


def test_retry_after_header_is_honoured(api, client, sleeps):
    route = api.get(f"/repos/{REPO}/pulls/7")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "3"}),
        pr_ok(),
    ]

    assert client.get_pull_request(REPO, 7).title == "ok"
    assert sleeps == [3.0]


def test_primary_rate_limit_waits_until_reset(api, client, sleeps):
    reset = str(int(time.time()) + 5)
    route = api.get(f"/repos/{REPO}/pulls/7")
    route.side_effect = [
        httpx.Response(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset}),
        pr_ok(),
    ]

    assert client.get_pull_request(REPO, 7).title == "ok"
    assert 4 < sleeps[0] <= 7


def test_rate_limit_longer_than_cap_fails_fast(api, client, sleeps):
    api.get(f"/repos/{REPO}/pulls/7").respond(429, headers={"Retry-After": "3600"})

    with pytest.raises(GitHubError, match="rate limited"):
        client.get_pull_request(REPO, 7)

    assert sleeps == []


def test_get_pull_request_returns_metadata(api, client):
    api.get(f"/repos/{REPO}/pulls/7").respond(
        200, json={"title": "Add limiter", "body": None, "head": {"sha": "abc123"}}
    )

    info = client.get_pull_request(REPO, 7)

    assert (info.title, info.body, info.head_sha) == ("Add limiter", None, "abc123")


def test_list_repo_files_keeps_only_blobs_and_reports_truncation(api, client):
    route = api.get(f"/repos/{REPO}/git/trees/abc123").respond(
        200,
        json={
            "truncated": True,
            "tree": [
                {"path": "src", "type": "tree"},
                {"path": "src/app.py", "type": "blob"},
                {"path": "tests/test_app.py", "type": "blob"},
            ],
        },
    )

    tree = client.list_repo_files(REPO, "abc123")

    assert tree.paths == ["src/app.py", "tests/test_app.py"]
    assert tree.truncated
    assert route.calls.last.request.url.params["recursive"] == "1"
