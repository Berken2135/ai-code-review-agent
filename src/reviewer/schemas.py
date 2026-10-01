"""Pydantic schemas shared across layers (webhook payloads, GitHub API objects)."""

from pydantic import BaseModel


class _Head(BaseModel):
    sha: str


class _PullRequest(BaseModel):
    number: int
    draft: bool = False
    head: _Head


class _Repository(BaseModel):
    full_name: str


class PullRequestEvent(BaseModel):
    """The subset of GitHub's `pull_request` webhook payload the reviewer needs."""

    action: str
    pull_request: _PullRequest
    repository: _Repository


class PullRequestInfo(BaseModel):
    """PR metadata the agent needs as (untrusted) prompt context."""

    title: str
    body: str | None = None
    head_sha: str


class RepoTree(BaseModel):
    paths: list[str]
    truncated: bool = False  # GitHub caps very large trees


class ChangedFile(BaseModel):
    """One entry of GitHub's "list pull request files" response."""

    filename: str
    status: str
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    patch: str | None = None  # absent for binary or very large files
