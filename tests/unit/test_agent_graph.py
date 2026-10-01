"""Graph-level behaviour: wiring, LLM-call collection and both failure policies."""

import pytest

from reviewer.agent.graph import ReviewFailedError, run_review
from reviewer.github.client import GitHubError
from reviewer.llm.base import LLMError
from reviewer.llm.fake import FakeLLM
from reviewer.services.comment_formatter import format_comment
from tests.fakes import SHA, FakeGitHub, changed_file, finding_json

FILE_LINES = [f"line_{i} = {i}" for i in range(1, 11)]

CODE_REVIEW = {
    "findings": [finding_json(severity="high", title="Real bug", line_start=3, line_end=4)]
}
TEST_ANALYSIS = {
    "suggestions": [
        {
            "title": "Cover the bug",
            "description": "No test hits it.",
            "file_path": "src/app.py",
            "test_file": "tests/test_app.py",
            "framework": "pytest",
            "test_code": "def test_bug():\n    assert True",
            "confidence": 0.8,
        }
    ]
}
FINAL = {"verdict": "request_changes", "summary": "One real bug.", "ranked_ids": [1, 2]}


def github() -> FakeGitHub:
    return FakeGitHub(
        files=[changed_file("src/app.py", FILE_LINES)],
        tree=["src/app.py", "tests/test_app.py"],
        contents={"tests/test_app.py": "def test_existing(): ..."},
    )


def run(gh: FakeGitHub, llm: FakeLLM):
    return run_review(gh, llm, repo="octo/repo", pr_number=7, head_sha=SHA)


def test_happy_path_runs_all_four_nodes_and_collects_every_llm_call():
    llm = FakeLLM([CODE_REVIEW, TEST_ANALYSIS, FINAL])

    state = run(github(), llm)

    assert state["node_status"] == {
        "fetch_context": "ok",
        "code_review": "ok",
        "test_analysis": "ok",
        "final_review": "ok",
    }
    assert [row["node"] for row in state["llm_calls"]] == [
        "code_review",
        "test_analysis",
        "final_review",
    ]
    assert all(row["error"] is None and row["provider"] == "fake" for row in state["llm_calls"])
    assert [c.method for c in llm.calls] == ["complete_structured"] * 3
    review = state["review"]
    assert review.verdict == "request_changes"
    assert [f.category for f in review.findings] == ["bug", "missing_test"]
    assert not state.get("fatal_error")


def test_test_analysis_failure_still_ships_the_review_without_test_findings():
    llm = FakeLLM([CODE_REVIEW, LLMError("test model exploded"), FINAL])

    state = run(github(), llm)

    assert state["node_status"]["test_analysis"] == "failed"
    assert state["node_status"]["final_review"] == "ok"
    assert [f.category for f in state["review"].findings] == ["bug"]
    assert state["errors"] and "test_analysis failed" in state["errors"][0]
    rows = {row["node"]: row for row in state["llm_calls"]}
    assert rows["test_analysis"]["error"].startswith("LLMError")
    assert "Test analysis failed, so this review has no test suggestions." in format_comment(state)


def test_code_review_failure_fails_the_run_with_a_clear_error_and_keeps_the_calls():
    llm = FakeLLM([LLMError("provider down")])

    with pytest.raises(ReviewFailedError, match="code_review failed on every chunk") as exc_info:
        run(github(), llm)

    state = exc_info.value.state
    assert len(llm.calls) == 1  # test_analysis and final_review never ran
    assert state["node_status"]["code_review"] == "failed"
    assert "test_analysis" not in state["node_status"]
    assert [row["node"] for row in state["llm_calls"]] == ["code_review"]
    assert state["llm_calls"][0]["error"].startswith("LLMError")
    assert state.get("review") is None


def test_fetch_context_failure_fails_the_run_before_any_llm_call():
    gh = FakeGitHub(fail_on={"get_pull_request": GitHubError("GitHub is down", 503)})
    llm = FakeLLM()

    with pytest.raises(
        ReviewFailedError, match="fetch_context failed: GitHubError: GitHub is down"
    ):
        run(gh, llm)

    assert llm.calls == []


def test_final_review_llm_failure_degrades_instead_of_failing():
    llm = FakeLLM([CODE_REVIEW, TEST_ANALYSIS, LLMError("summary model down")])

    state = run(github(), llm)

    assert state["node_status"]["final_review"] == "degraded"
    assert state["review"].verdict == "request_changes"
    assert "final ranking step failed" in format_comment(state)


def test_partial_code_review_failure_ships_with_a_coverage_note():
    files = [changed_file(f"src/m{i}.py", FILE_LINES * 20) for i in range(2)]
    gh = FakeGitHub(files=files)
    from reviewer.agent.config import AgentConfig

    llm = FakeLLM([{"findings": []}, LLMError("second chunk failed"), {"suggestions": []}])
    state = run_review(
        gh,
        llm,
        repo="octo/repo",
        pr_number=7,
        head_sha=SHA,
        config=AgentConfig(chunk_char_budget=3_000),
    )

    assert state["node_status"]["code_review"] == "degraded"
    assert "could not be reviewed" in format_comment(state)


def test_pr_with_only_lockfiles_needs_no_llm_at_all():
    gh = FakeGitHub(files=[changed_file("package-lock.json")])
    llm = FakeLLM()

    state = run(gh, llm)

    assert llm.calls == []
    assert state["review"].verdict == "approve"
    assert state["node_status"]["code_review"] == "skipped"
    assert "No reviewable changes" in format_comment(state)


def test_unexpected_node_exception_is_contained_and_reported():
    class Exploding(FakeGitHub):
        def list_changed_files(self, repo, number):
            raise ValueError("bug in our code")

    with pytest.raises(ReviewFailedError, match="ValueError: bug in our code"):
        run(Exploding(), FakeLLM())
