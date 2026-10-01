"""GitHub REST client: retries with backoff and rate-limit awareness."""

import time
from collections.abc import Callable
from typing import Any, Self
from urllib.parse import quote

import httpx
import structlog
from pydantic import SecretStr
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from reviewer.github.auth import auth_headers
from reviewer.schemas import ChangedFile, PullRequestInfo, RepoTree

API_URL = "https://api.github.com"
BOT_MARKER = "<!-- pr-review-agent -->"
MAX_RATE_LIMIT_WAIT = 60.0  # seconds; a longer rate-limit window fails the call instead of blocking
PAGE_SIZE = 100
MAX_PAGES = 30  # GitHub lists at most 3000 files per PR

log = structlog.get_logger(__name__)


class GitHubError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class _RetryableError(GitHubError):
    def __init__(self, message: str, status_code: int, retry_after: float | None = None):
        super().__init__(message, status_code)
        self.retry_after = retry_after


def _rate_limit_delay(response: httpx.Response) -> float | None:
    """Seconds to wait if the response is a (primary or secondary) rate limit, else None."""
    if response.status_code not in (403, 429):
        return None
    if (retry_after := response.headers.get("retry-after")) is not None:
        return float(retry_after)
    if response.headers.get("x-ratelimit-remaining") == "0":
        reset = response.headers.get("x-ratelimit-reset")
        return max(0.0, float(reset) - time.time()) + 1 if reset else MAX_RATE_LIMIT_WAIT
    return None


class GitHubClient:
    def __init__(
        self,
        token: SecretStr | str | None,
        *,
        base_url: str = API_URL,
        max_attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._http = httpx.Client(
            base_url=base_url,
            headers={
                **auth_headers(token),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ai-code-review-agent",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._backoff = wait_exponential(multiplier=0.5, max=8)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- public API -------------------------------------------------------------------------

    def get_pull_request(self, repo: str, number: int) -> PullRequestInfo:
        data = self._request("GET", f"/repos/{repo}/pulls/{number}").json()
        return PullRequestInfo(
            title=data["title"], body=data.get("body"), head_sha=data["head"]["sha"]
        )

    def list_repo_files(self, repo: str, ref: str) -> RepoTree:
        """All file paths at a commit (one API call; GitHub flags very large trees as truncated)."""
        data = self._request("GET", f"/repos/{repo}/git/trees/{ref}", params={"recursive": "1"})
        body = data.json()
        return RepoTree(
            paths=[e["path"] for e in body.get("tree", []) if e.get("type") == "blob"],
            truncated=bool(body.get("truncated")),
        )

    def list_changed_files(self, repo: str, number: int) -> list[ChangedFile]:
        items = self._get_all(f"/repos/{repo}/pulls/{number}/files")
        return [ChangedFile.model_validate(item) for item in items]

    def get_file_content(self, repo: str, path: str, ref: str) -> str | None:
        """File content at a commit SHA, or None if the file does not exist there."""
        try:
            response = self._request(
                "GET",
                f"/repos/{repo}/contents/{quote(path, safe='/')}",
                params={"ref": ref},
                headers={"Accept": "application/vnd.github.raw+json"},
            )
        except GitHubError as exc:
            if exc.status_code == 404:
                return None
            raise
        return response.text

    def upsert_bot_comment(self, repo: str, number: int, body: str) -> int:
        """Create the bot's PR comment, or update it if one already exists. Returns its id."""
        if not body.startswith(BOT_MARKER):
            body = f"{BOT_MARKER}\n{body}"
        # ponytail: scans every comment on each call; fine below a few hundred comments.
        for comment in self._get_all(f"/repos/{repo}/issues/{number}/comments"):
            if (comment.get("body") or "").startswith(BOT_MARKER):
                self._request(
                    "PATCH", f"/repos/{repo}/issues/comments/{comment['id']}", json={"body": body}
                )
                return comment["id"]
        response = self._request(
            "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body}
        )
        return response.json()["id"]

    # --- internals --------------------------------------------------------------------------

    def _get_all(self, url: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            batch = self._request("GET", url, params={"per_page": PAGE_SIZE, "page": page}).json()
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        return items

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        retrying = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=self._wait,
            retry=retry_if_exception_type((_RetryableError, httpx.TransportError)),
            before_sleep=self._log_retry,
            sleep=self._sleep,
            reraise=True,
        )
        try:
            return retrying(self._send_once, method, url, **kwargs)
        except httpx.TransportError as exc:
            raise GitHubError(f"GitHub request failed: {exc!r}") from exc

    def _send_once(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        response = self._http.request(method, url, **kwargs)
        if response.is_success:
            return response

        message = f"GitHub {method} {url} -> {response.status_code}: {response.text[:200]}"
        delay = _rate_limit_delay(response)
        if delay is not None and delay > MAX_RATE_LIMIT_WAIT:
            raise GitHubError(f"rate limited for {delay:.0f}s; giving up. {message}", 429)
        if delay is not None or response.status_code == 429 or response.status_code >= 500:
            raise _RetryableError(message, response.status_code, delay)
        raise GitHubError(message, response.status_code)

    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, _RetryableError) and exc.retry_after is not None:
            return exc.retry_after
        return self._backoff(state)

    @staticmethod
    def _log_retry(state: RetryCallState) -> None:
        log.warning(
            "github_request_retry",
            attempt=state.attempt_number,
            error=repr(state.outcome.exception()) if state.outcome else None,
            wait_seconds=state.next_action.sleep if state.next_action else None,
        )
