"""Test doubles and builders for the agent tests. No network, no real clients."""

import json
from pathlib import Path
from typing import Any

from reviewer.agent.state import DiffChunk, ReviewState
from reviewer.llm.fake import FakeLLM
from reviewer.schemas import ChangedFile, PullRequestInfo, RepoTree
from reviewer.services.review_service import Dependencies

SHA = "a" * 40
COMMENT_ID = 4242
SAMPLE = Path(__file__).parent / "fixtures" / "sample_pr"


def added_patch(lines: list[str], start: int = 1) -> str:
    """A patch that adds `lines` starting at new-file line `start`."""
    return f"@@ -0,0 +{start},{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines)


def changed_file(
    path: str, lines: list[str] | None = None, status: str = "modified", patch: Any = "auto"
) -> ChangedFile:
    lines = lines if lines is not None else ["x = 1", "y = 2"]
    body = added_patch(lines) if patch == "auto" else patch
    return ChangedFile(
        filename=path,
        status=status,
        additions=len(lines),
        deletions=0,
        changes=len(lines),
        patch=body,
    )


class FakeGitHub:
    """In-memory GitHubPort. `fail_on` maps a method name to an exception to raise."""

    def __init__(
        self,
        *,
        files: list[ChangedFile] | None = None,
        tree: list[str] | None = None,
        contents: dict[str, str] | None = None,
        title: str = "Add feature",
        body: str | None = "Adds a feature.",
        head_sha: str = SHA,
        tree_truncated: bool = False,
        fail_on: dict[str, Exception] | None = None,
    ) -> None:
        self.files = files if files is not None else [changed_file("src/app.py")]
        self.tree = tree or []
        self.contents = contents or {}
        self.title, self.body, self.head_sha = title, body, head_sha
        self.tree_truncated = tree_truncated
        self.fail_on = fail_on or {}
        self.calls: list[tuple[str, ...]] = []
        self.comments: list[tuple[str, int, str]] = []  # (repo, pr number, body)

    def _enter(self, name: str, *args: str) -> None:
        self.calls.append((name, *args))
        if name in self.fail_on:
            raise self.fail_on[name]

    def get_pull_request(self, repo: str, number: int) -> PullRequestInfo:
        self._enter("get_pull_request")
        return PullRequestInfo(title=self.title, body=self.body, head_sha=self.head_sha)

    def list_changed_files(self, repo: str, number: int) -> list[ChangedFile]:
        self._enter("list_changed_files")
        return self.files

    def list_repo_files(self, repo: str, ref: str) -> RepoTree:
        self._enter("list_repo_files")
        return RepoTree(paths=self.tree, truncated=self.tree_truncated)

    def get_file_content(self, repo: str, path: str, ref: str) -> str | None:
        self._enter("get_file_content", path)
        return self.contents.get(path)

    def upsert_bot_comment(self, repo: str, number: int, body: str) -> int:
        self._enter("upsert_bot_comment")
        self.comments.append((repo, number, body))
        return COMMENT_ID


def make_state(**overrides: Any) -> ReviewState:
    """A post-fetch_context state with one reviewable file, for testing the LLM nodes."""
    state: ReviewState = {
        "repo": "octo/repo",
        "pr_number": 1,
        "head_sha": SHA,
        "pr_title": "Add feature",
        "pr_body": "Adds a feature.",
        "reviewed_files": ["src/app.py"],
        "skipped_files": {},
        "line_ranges": {"src/app.py": [(1, 20)]},
        "chunks": [
            DiffChunk(index=0, files=["src/app.py"], text="### File: src/app.py\n    1 +x = 1")
        ],
        "existing_tests": {},
        "code_findings": [],
        "test_findings": [],
        "errors": [],
        "llm_calls": [],
        "node_status": {},
        "validation": {},
    }
    state.update(overrides)
    return state


def finding_json(**overrides: Any) -> dict[str, Any]:
    finding = {
        "category": "bug",
        "severity": "medium",
        "file_path": "src/app.py",
        "line_start": 1,
        "line_end": 2,
        "title": "Something is wrong",
        "description": "Explanation.",
        "suggestion": "Fix it.",
        "confidence": 0.9,
    }
    finding.update(overrides)
    return finding


def load_sample(name: str) -> Any:
    return json.loads((SAMPLE / name).read_text(encoding="utf-8"))


def sample_github(**kwargs: Any) -> FakeGitHub:
    """FakeGitHub serving the fixture PR in tests/fixtures/sample_pr (octo-org/orders #17)."""
    pr = load_sample("pr.json")
    return FakeGitHub(
        files=[ChangedFile(**f) for f in load_sample("files.json")],
        tree=load_sample("tree.json"),
        contents={
            "tests/test_service.py": (SAMPLE / "repo/tests/test_service.py").read_text(
                encoding="utf-8"
            )
        },
        title=pr["title"],
        body=pr["body"],
        head_sha=pr["head_sha"],
        **kwargs,
    )


def sample_llm(*extra: Any) -> FakeLLM:
    """FakeLLM scripted with the sample PR's three responses (code, tests, final)."""
    return FakeLLM(
        [
            load_sample("llm_code_review.json"),
            load_sample("llm_test_analysis.json"),
            load_sample("llm_final_review.json"),
            *extra,
        ]
    )


def clean_dependencies() -> Dependencies:
    """Fake clients for a one-file PR with nothing to report (used by webhook-flow tests)."""
    return Dependencies(FakeGitHub(), FakeLLM([{"findings": []}, {"suggestions": []}]))
