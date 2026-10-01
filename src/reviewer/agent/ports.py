"""What the agent needs from GitHub. `GitHubClient` satisfies this; tests use a fake."""

from typing import Protocol

from reviewer.schemas import ChangedFile, PullRequestInfo, RepoTree


class GitHubPort(Protocol):
    def get_pull_request(self, repo: str, number: int) -> PullRequestInfo: ...

    def list_changed_files(self, repo: str, number: int) -> list[ChangedFile]: ...

    def list_repo_files(self, repo: str, ref: str) -> RepoTree: ...

    def get_file_content(self, repo: str, path: str, ref: str) -> str | None: ...
