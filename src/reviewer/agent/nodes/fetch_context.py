"""fetch_context (no LLM): PR metadata, filtered and chunked diff, existing tests."""

import contextlib

import structlog

from reviewer.agent.config import AgentConfig
from reviewer.agent.diff import file_section, pack_chunks, parse_hunks
from reviewer.agent.files import (
    JS_EXTENSIONS,
    detect_js_test_framework,
    find_test_files,
    is_testable_source,
    review_priority,
    skip_reason,
)
from reviewer.agent.nodes.common import NodeFn, guarded
from reviewer.agent.ports import GitHubPort
from reviewer.agent.state import DiffChunk, ReviewState
from reviewer.github.client import GitHubError

log = structlog.get_logger(__name__)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[... truncated ...]"


def make_fetch_context(github: GitHubPort, config: AgentConfig) -> NodeFn:
    @guarded("fetch_context", critical=True)
    def fetch_context(state: ReviewState) -> dict:
        repo, number = state["repo"], state["pr_number"]
        info = github.get_pull_request(repo, number)
        files = github.list_changed_files(repo, number)
        notes: list[str] = []
        if info.head_sha != state.get("head_sha"):
            notes.append(f"PR head moved to {info.head_sha[:7]}; reviewing the newest commit.")

        # --- filter and order ---
        skipped: dict[str, str] = {}
        reviewable = []
        for file in files:
            reason = skip_reason(file)
            if reason:
                skipped[file.filename] = reason
            else:
                reviewable.append(file)
        reviewable.sort(key=lambda f: (review_priority(f.filename), f.filename))

        # --- chunk under the budget ---
        packed = pack_chunks(
            [(f.filename, file_section(f)) for f in reviewable],
            config.chunk_char_budget,
            config.max_chunks,
        )
        chunks = [
            DiffChunk(index=i, files=paths, text=text)
            for i, (paths, text) in enumerate(packed.chunks)
        ]
        if packed.truncated_files:
            notes.append(
                f"{len(packed.truncated_files)} large file diff(s) were truncated: "
                + ", ".join(packed.truncated_files[:5])
            )
        if packed.omitted_files:
            notes.append(
                f"{len(packed.omitted_files)} file(s) were not reviewed because the diff is too "
                "large: " + ", ".join(packed.omitted_files[:5])
            )

        # --- existing tests, by naming convention ---
        existing_tests, framework = _load_test_context(
            github, config, repo, info.head_sha, [f.filename for f in reviewable], notes
        )

        return {
            "pr_title": info.title,
            "pr_body": _truncate(info.body or "", config.pr_body_char_budget),
            "head_sha": info.head_sha,
            "changed_files": files,
            "reviewed_files": [f.filename for f in reviewable],
            "skipped_files": skipped,
            "line_ranges": {f.filename: parse_hunks(f.patch or "") for f in reviewable},
            "chunks": chunks,
            "diff_truncated": bool(packed.truncated_files or packed.omitted_files),
            "context_notes": notes,
            "existing_tests": existing_tests,
            "js_test_framework": framework,
            "node_status": {"fetch_context": "ok"},
        }

    return fetch_context


def _load_test_context(
    github: GitHubPort,
    config: AgentConfig,
    repo: str,
    sha: str,
    reviewed: list[str],
    notes: list[str],
) -> tuple[dict[str, str], str | None]:
    """Existing test files for the changed sources, and the JS test framework if relevant.

    Enrichment only: a failure here degrades the review (with a note) instead of failing it.
    """
    sources = [p for p in reviewed if is_testable_source(p)]
    if not sources:
        return {}, None

    test_paths: list[str] = []
    try:
        tree = github.list_repo_files(repo, sha)
        for source in sources:
            for test_path in find_test_files(source, tree.paths):
                if test_path not in test_paths:
                    test_paths.append(test_path)
        if tree.truncated:
            notes.append("Repository listing was truncated by GitHub; some tests may be missed.")
    except GitHubError as exc:
        notes.append(f"Could not list repository files, existing tests were not considered: {exc}")

    tests: dict[str, str] = {}
    for path in test_paths[: config.max_test_files]:
        try:
            content = github.get_file_content(repo, path, sha)
        except GitHubError:
            continue
        if content is not None:
            tests[path] = _truncate(content, config.test_file_char_budget)

    framework = None
    if any(p.endswith(JS_EXTENSIONS) for p in sources):
        with contextlib.suppress(GitHubError):
            framework = detect_js_test_framework(github.get_file_content(repo, "package.json", sha))
    return tests, framework
