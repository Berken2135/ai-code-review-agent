"""Golden-file test: a whole sample PR through the graph, compared to a stored comment.

Regenerate after an intentional formatting or prompt-logic change:
    UPDATE_GOLDEN=1 pytest tests/integration/test_review_golden.py
and review the diff of expected_comment.md before accepting it.
"""

import os

from reviewer.agent.graph import run_review
from reviewer.llm.fake import FakeLLM
from reviewer.services.comment_formatter import GITHUB_COMMENT_LIMIT, format_comment
from tests.fakes import SAMPLE, load_sample, sample_github, sample_llm


def run_sample() -> tuple[dict, FakeLLM]:
    pr = load_sample("pr.json")
    llm = sample_llm()
    state = run_review(
        sample_github(), llm, repo=pr["repo"], pr_number=pr["number"], head_sha=pr["head_sha"]
    )
    return state, llm


def test_sample_pr_produces_the_expected_comment():
    state, _ = run_sample()

    comment = format_comment(state)

    golden = SAMPLE / "expected_comment.md"
    if os.getenv("UPDATE_GOLDEN"):
        golden.write_text(comment, encoding="utf-8", newline="\n")
    assert comment == golden.read_text(encoding="utf-8")
    assert len(comment) < GITHUB_COMMENT_LIMIT


def test_sample_pr_post_processing_did_what_the_comment_shows():
    state, llm = run_sample()

    assert state["skipped_files"] == {"package-lock.json": "lockfile"}
    assert state["reviewed_files"] == ["src/orders/service.py", "README.md"]  # source before docs
    assert list(state["existing_tests"]) == ["tests/test_service.py"]  # mapped by naming convention
    validation = state["validation"]
    assert validation["code_review"] == {
        "dropped_unknown_file": 1,  # src/orders/ghost.py does not exist in the PR
        "fixed_path": 1,  # b/src/orders/service.py -> src/orders/service.py
        "clamped_lines": 1,  # line_end 40 -> 10
    }
    assert validation["test_analysis"]["dropped_unknown_file"] == 1
    assert validation["final_review"]["dropped_low_confidence"] == 1
    assert validation["final_review"]["dropped_duplicates"] == 1
    review = state["review"]
    assert (
        review.verdict == "request_changes"
    )  # the model said "approve"; a high finding forbids it
    # severity first, then confidence: the 85% test suggestion outranks the 80% bug (both medium)
    assert [f.category for f in review.findings] == ["security", "missing_test", "bug"]
    assert [row["node"] for row in state["llm_calls"]] == [
        "code_review",
        "test_analysis",
        "final_review",
    ]
    # The injected instruction in the PR body reached the model only as untrusted data.
    for call in llm.calls[:2]:
        assert "IGNORE ALL PREVIOUS INSTRUCTIONS" not in call.system
        assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in call.user
