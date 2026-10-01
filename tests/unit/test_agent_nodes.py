"""Per-node behaviour with FakeLLM and FakeGitHub. No network."""

import json

import pytest

from reviewer.agent.config import AgentConfig
from reviewer.agent.nodes.code_review import make_code_review
from reviewer.agent.nodes.fetch_context import make_fetch_context
from reviewer.agent.nodes.final_review import make_final_review
from reviewer.agent.nodes.test_analysis import make_test_analysis
from reviewer.agent.state import Finding, TestSuggestion
from reviewer.github.client import GitHubError
from reviewer.llm.base import LLMError, LLMRateLimitError
from reviewer.llm.fake import FakeLLM
from tests.fakes import SHA, FakeGitHub, changed_file, finding_json, make_state


def fetch(github: FakeGitHub, config: AgentConfig | None = None, **state) -> dict:
    initial = {"repo": "octo/repo", "pr_number": 7, "head_sha": SHA, **state}
    return make_fetch_context(github, config or AgentConfig())(initial)


def finding(**overrides) -> Finding:
    return Finding(**finding_json(**overrides))


# --- fetch_context -----------------------------------------------------------------------------


def test_fetch_context_filters_files_and_orders_source_before_docs():
    github = FakeGitHub(
        files=[
            changed_file("README.md"),
            changed_file("package-lock.json"),
            changed_file("dist/app.js"),
            changed_file("logo.png", patch=None),
            changed_file("src/app.py"),
        ]
    )

    update = fetch(github)

    assert update["node_status"] == {"fetch_context": "ok"}
    assert update["reviewed_files"] == ["src/app.py", "README.md"]
    assert update["skipped_files"] == {
        "package-lock.json": "lockfile",
        "dist/app.js": "generated",
        "logo.png": "binary",
    }
    assert update["chunks"][0].files == ["src/app.py", "README.md"]
    assert update["pr_title"] == "Add feature"


def test_fetch_context_records_line_ranges_and_numbers_the_diff():
    github = FakeGitHub(files=[changed_file("src/app.py", ["a", "b", "c"])])

    update = fetch(github)

    assert update["line_ranges"] == {"src/app.py": [(1, 3)]}
    assert "    2 +b" in update["chunks"][0].text


def test_fetch_context_flags_a_truncated_diff():
    github = FakeGitHub(files=[changed_file("src/big.py", [f"x{i} = {i}" for i in range(300)])])

    update = fetch(github, AgentConfig(chunk_char_budget=500))

    assert update["diff_truncated"] is True
    assert any("truncated" in note for note in update["context_notes"])
    assert len(update["chunks"][0].text) <= 500


def test_fetch_context_flags_files_left_out_of_a_huge_diff():
    files = [changed_file(f"src/m{i}.py", [f"v{j} = {j}" for j in range(40)]) for i in range(4)]

    update = fetch(FakeGitHub(files=files), AgentConfig(chunk_char_budget=700, max_chunks=1))

    assert update["diff_truncated"] is True
    assert len(update["chunks"]) == 1
    assert any("not reviewed" in note for note in update["context_notes"])


def test_fetch_context_maps_changed_modules_to_existing_tests():
    github = FakeGitHub(
        files=[changed_file("src/orders.py")],
        tree=["src/orders.py", "tests/test_orders.py", "tests/test_other.py"],
        contents={"tests/test_orders.py": "def test_x(): ...", "tests/test_other.py": "nope"},
    )

    update = fetch(github)

    assert update["existing_tests"] == {"tests/test_orders.py": "def test_x(): ..."}
    assert ("get_file_content", "package.json") not in github.calls  # Python only


def test_fetch_context_detects_the_js_test_framework():
    github = FakeGitHub(
        files=[changed_file("web/button.ts")],
        tree=["web/button.ts", "web/button.test.ts"],
        contents={
            "web/button.test.ts": "test('x', () => {})",
            "package.json": '{"devDependencies": {"vitest": "^1.0.0"}}',
        },
    )

    update = fetch(github)

    assert update["js_test_framework"] == "vitest"
    assert "web/button.test.ts" in update["existing_tests"]


def test_fetch_context_skips_test_lookup_when_no_source_changed():
    github = FakeGitHub(files=[changed_file("README.md")])

    update = fetch(github)

    assert update["existing_tests"] == {}
    assert not any(call[0] == "list_repo_files" for call in github.calls)


def test_fetch_context_degrades_when_the_repo_listing_fails():
    github = FakeGitHub(fail_on={"list_repo_files": GitHubError("boom", 500)})

    update = fetch(github)

    assert update["node_status"] == {"fetch_context": "ok"}
    assert update["existing_tests"] == {}
    assert any("existing tests were not considered" in n for n in update["context_notes"])


def test_fetch_context_failure_is_fatal_and_clear():
    github = FakeGitHub(fail_on={"list_changed_files": GitHubError("GitHub is down", 503)})

    update = fetch(github)

    assert update["node_status"] == {"fetch_context": "failed"}
    assert "fetch_context failed: GitHubError: GitHub is down" in update["fatal_error"]
    assert update["errors"] == [update["fatal_error"]]


def test_fetch_context_notes_when_the_pr_head_moved():
    update = fetch(FakeGitHub(head_sha="b" * 40))

    assert update["head_sha"] == "b" * 40
    assert any("head moved" in note for note in update["context_notes"])


def test_fetch_context_caps_a_huge_pr_description():
    update = fetch(FakeGitHub(body="x" * 50_000), AgentConfig(pr_body_char_budget=100))

    assert len(update["pr_body"]) < 200


def test_fetch_context_with_only_skipped_files_yields_no_chunks():
    update = fetch(FakeGitHub(files=[changed_file("package-lock.json")]))

    assert update["chunks"] == []
    assert update["reviewed_files"] == []


# --- code_review -------------------------------------------------------------------------------


def two_chunk_state():
    from reviewer.agent.state import DiffChunk

    return make_state(
        reviewed_files=["src/a.py", "src/b.py"],
        line_ranges={"src/a.py": [(1, 20)], "src/b.py": [(1, 20)]},
        chunks=[
            DiffChunk(index=0, files=["src/a.py"], text="### File: src/a.py"),
            DiffChunk(index=1, files=["src/b.py"], text="### File: src/b.py"),
        ],
    )


def test_code_review_runs_once_per_chunk_and_merges_findings():
    llm = FakeLLM(
        [
            {"findings": [finding_json(file_path="src/a.py", title="A")]},
            {"findings": [finding_json(file_path="src/b.py", title="B")]},
        ]
    )

    update = make_code_review(llm)(two_chunk_state())

    assert len(llm.calls) == 2
    assert [f.title for f in update["code_findings"]] == ["A", "B"]
    assert update["node_status"] == {"code_review": "ok"}
    assert [row["node"] for row in update["llm_calls"]] == ["code_review", "code_review"]


def test_code_review_applies_reference_validation():
    llm = FakeLLM(
        [
            {
                "findings": [
                    finding_json(title="real", line_start=999, line_end=1000),
                    finding_json(title="ghost", file_path="src/ghost.py"),
                ]
            }
        ]
    )

    update = make_code_review(llm)(make_state())

    assert [f.title for f in update["code_findings"]] == ["real"]
    assert (update["code_findings"][0].line_start, update["code_findings"][0].line_end) == (20, 20)
    assert update["validation"]["code_review"]["dropped_unknown_file"] == 1
    assert update["validation"]["code_review"]["clamped_lines"] == 1


def test_code_review_partial_chunk_failure_degrades_but_ships():
    llm = FakeLLM([{"findings": [finding_json(file_path="src/a.py")]}, LLMRateLimitError("429")])

    update = make_code_review(llm)(two_chunk_state())

    assert update["node_status"] == {"code_review": "degraded"}
    assert update["chunks_failed"] == 1
    assert "fatal_error" not in update
    assert "1 of 2 chunks failed" in update["errors"][0]
    failed_row = update["llm_calls"][1]
    assert failed_row["error"].startswith("LLMRateLimitError")


def test_code_review_fails_when_every_chunk_fails():
    llm = FakeLLM([LLMError("boom"), LLMError("boom")])

    update = make_code_review(llm)(two_chunk_state())

    assert update["node_status"] == {"code_review": "failed"}
    assert "every chunk" in update["fatal_error"]
    assert len(update["llm_calls"]) == 2  # failed calls are still recorded


def test_code_review_skips_when_there_is_nothing_to_review():
    llm = FakeLLM()

    update = make_code_review(llm)(make_state(chunks=[], reviewed_files=[]))

    assert update["node_status"] == {"code_review": "skipped"}
    assert llm.calls == []


def test_code_review_sends_pr_text_only_as_untrusted_data():
    llm = FakeLLM([{"findings": []}])

    make_code_review(llm)(make_state(pr_title="Fix bug", pr_body="details"))

    call = llm.calls[0]
    assert "Fix bug" in call.user and "Fix bug" not in call.system
    assert "<<<BEGIN UNTRUSTED PR TITLE AND DESCRIPTION" in call.user
    assert "<<<BEGIN UNTRUSTED DIFF" in call.user


# --- test_analysis -----------------------------------------------------------------------------


def suggestion_json(**overrides):
    data = {
        "title": "Test the edge case",
        "description": "Nothing covers it.",
        "severity": "medium",
        "file_path": "src/app.py",
        "test_file": "tests/test_app.py",
        "framework": "pytest",
        "test_code": "def test_edge():\n    assert True",
        "confidence": 0.8,
    }
    return {**data, **overrides}


def test_test_analysis_prompt_carries_diff_existing_tests_and_findings_as_data():
    llm = FakeLLM([{"suggestions": []}])
    state = make_state(
        existing_tests={"tests/test_app.py": "def test_old(): ..."},
        code_findings=[finding(title="Off by one")],
        js_test_framework="vitest",
    )

    make_test_analysis(llm, AgentConfig())(state)

    user = llm.calls[0].user
    assert "def test_old(): ..." in user
    assert "Off by one" in user
    assert "use vitest" in user
    assert "<<<BEGIN UNTRUSTED EXISTING TEST FILES" in user
    assert "<<<BEGIN UNTRUSTED AUTOMATED FINDINGS" in user
    assert "Off by one" not in llm.calls[0].system


def test_test_analysis_validates_filters_and_caps_suggestions():
    llm = FakeLLM(
        [
            {
                "suggestions": [
                    suggestion_json(title="keep"),
                    suggestion_json(title="ghost", file_path="src/ghost.py"),
                    suggestion_json(title="unsure", confidence=0.1),
                    suggestion_json(title="escape", test_file="../../evil.py"),
                    suggestion_json(title="over the cap"),
                ]
            }
        ]
    )

    update = make_test_analysis(llm, AgentConfig(max_test_suggestions=2))(make_state())

    suggestions = update["test_findings"]
    assert [s.title for s in suggestions] == ["keep", "escape"]
    assert suggestions[1].test_file is None
    assert update["validation"]["test_analysis"]["dropped_unknown_file"] == 1
    assert update["llm_calls"][0]["node"] == "test_analysis"


def test_test_analysis_skips_when_only_docs_changed():
    llm = FakeLLM()
    state = make_state(reviewed_files=["README.md"])

    update = make_test_analysis(llm, AgentConfig())(state)

    assert update["node_status"] == {"test_analysis": "skipped"}
    assert llm.calls == []


def test_test_analysis_failure_is_recorded_and_not_fatal():
    llm = FakeLLM([LLMError("provider down")])

    update = make_test_analysis(llm, AgentConfig())(make_state())

    assert update["node_status"] == {"test_analysis": "failed"}
    assert "fatal_error" not in update
    assert update["llm_calls"][0]["error"].startswith("LLMError")


# --- final_review ------------------------------------------------------------------------------


def decision(**overrides):
    return {
        "verdict": "comment",
        "summary": "A summary.",
        "ranked_ids": [],
        "drop_ids": [],
        **overrides,
    }


def run_final(llm, state=None, config=None):
    return make_final_review(llm, config or AgentConfig())(state or make_state())


def test_final_review_drops_low_confidence_before_the_model_sees_them():
    llm = FakeLLM([decision()])
    state = make_state(
        code_findings=[
            finding(title="sure", confidence=0.9),
            finding(title="unsure", confidence=0.2, line_start=9),
        ]
    )

    update = run_final(llm, state)

    assert "unsure" not in llm.calls[0].user
    assert [f.title for f in update["review"].findings] == ["sure"]
    assert update["validation"]["final_review"]["dropped_low_confidence"] == 1


def test_final_review_merges_duplicates_keeping_the_more_severe_one():
    state = make_state(
        code_findings=[
            finding(
                title="SQL injection", severity="medium", line_start=5, line_end=6, confidence=0.9
            ),
            finding(
                title="Unsafe query", severity="high", line_start=6, line_end=7, confidence=0.7
            ),
        ]
    )

    update = run_final(FakeLLM([decision()]), state)

    assert [(f.title, f.severity) for f in update["review"].findings] == [("Unsafe query", "high")]
    assert update["validation"]["final_review"]["dropped_duplicates"] == 1


def test_final_review_ranking_reorders_within_a_severity_but_never_across():
    state = make_state(
        code_findings=[
            finding(title="high one", severity="high", line_start=1, line_end=1),
            finding(title="med A", severity="medium", line_start=3, line_end=3, confidence=0.9),
            finding(title="med B", severity="medium", line_start=5, line_end=5, confidence=0.8),
        ]
    )
    # ids after deterministic sort: 1=high one, 2=med A, 3=med B. The model tries to put low first.
    llm = FakeLLM([decision(ranked_ids=[3, 2, 1])])

    update = run_final(llm, state)

    assert [f.title for f in update["review"].findings] == ["high one", "med B", "med A"]


def test_final_review_model_cannot_drop_high_severity_and_unknown_ids_are_ignored():
    state = make_state(
        code_findings=[
            finding(title="critical", severity="high", line_start=1, line_end=1),
            finding(title="noise", severity="low", line_start=5, line_end=5),
        ]
    )
    llm = FakeLLM([decision(drop_ids=[1, 2, 99], ranked_ids=[42, 2, 1])])

    update = run_final(llm, state)

    assert [f.title for f in update["review"].findings] == [
        "critical"
    ]  # low was dropped, high kept
    assert update["validation"]["final_review"]["dropped_by_model"] == 1


def test_final_review_findings_the_model_does_not_mention_are_kept():
    state = make_state(
        code_findings=[
            finding(title="one", severity="medium", line_start=1, line_end=1),
            finding(title="two", severity="medium", line_start=5, line_end=5, confidence=0.8),
        ]
    )

    update = run_final(FakeLLM([decision(ranked_ids=[2])]), state)

    assert [f.title for f in update["review"].findings] == ["two", "one"]


def test_final_review_caps_findings_and_reports_how_many_were_omitted():
    state = make_state(
        code_findings=[finding(title=f"f{i}", line_start=i + 1, line_end=i + 1) for i in range(6)]
    )

    update = run_final(FakeLLM([decision()]), state, AgentConfig(max_findings=4))

    review = update["review"]
    assert len(review.findings) == 4
    assert review.omitted_findings == 2


@pytest.mark.parametrize(
    ("severity", "model_verdict", "expected"),
    [
        ("high", "approve", "request_changes"),  # cannot approve a change with a high finding
        ("high", "comment", "request_changes"),
        ("medium", "approve", "comment"),
        ("medium", "request_changes", "request_changes"),
        ("low", "request_changes", "comment"),
        ("low", "approve", "comment"),
    ],
)
def test_final_review_verdict_cannot_contradict_the_findings(severity, model_verdict, expected):
    state = make_state(code_findings=[finding(severity=severity)])

    update = run_final(FakeLLM([decision(verdict=model_verdict)]), state)

    assert update["review"].verdict == expected


def test_final_review_without_findings_approves_without_calling_the_model():
    llm = FakeLLM()

    update = run_final(llm)

    assert llm.calls == []
    assert update["review"].verdict == "approve"
    assert "No issues found" in update["review"].summary


def test_final_review_with_nothing_reviewable_says_so_without_calling_the_model():
    llm = FakeLLM()
    state = make_state(
        reviewed_files=[], chunks=[], skipped_files={"package-lock.json": "lockfile"}
    )

    update = run_final(llm, state)

    assert llm.calls == []
    assert "No reviewable changes" in update["review"].summary


def test_final_review_converts_test_suggestions_into_missing_test_findings():
    suggestion = TestSuggestion(**suggestion_json())
    state = make_state(test_findings=[suggestion])

    update = run_final(FakeLLM([decision()]), state)

    (finding_,) = update["review"].findings
    assert finding_.category == "missing_test"
    assert finding_.suggested_test_code.startswith("def test_edge")
    assert "tests/test_app.py" in finding_.suggestion


def test_final_review_falls_back_to_automatic_ranking_when_the_model_fails():
    state = make_state(
        code_findings=[
            finding(title="low", severity="low", line_start=9, line_end=9),
            finding(title="high", severity="high", line_start=1, line_end=1),
        ]
    )

    update = run_final(FakeLLM([LLMError("provider down")]), state)

    review = update["review"]
    assert update["node_status"] == {"final_review": "degraded"}
    assert [f.title for f in review.findings] == ["high", "low"]
    assert review.verdict == "request_changes"
    assert "Automatic summary" in review.summary
    assert update["llm_calls"][0]["error"].startswith("LLMError")


def test_final_review_summary_is_length_capped():
    update = run_final(
        FakeLLM([decision(summary="x" * 10_000)]),
        make_state(code_findings=[finding()]),
    )

    assert len(update["review"].summary) == 1500


def test_final_review_sends_findings_as_untrusted_json():
    llm = FakeLLM([decision()])

    run_final(llm, make_state(code_findings=[finding(title="Marker check")]))

    user = llm.calls[0].user
    assert "<<<BEGIN UNTRUSTED FINDINGS" in user
    payload = user.split("]>>>\n", 1)[1].split("\n<<<END", 1)[0]
    assert json.loads(payload)[0]["title"] == "Marker check"
