from reviewer.agent.state import Finding, Review
from reviewer.github.client import BOT_MARKER
from reviewer.services.comment_formatter import (
    GITHUB_COMMENT_LIMIT,
    SAFE_LIMIT,
    escape,
    fenced,
    format_comment,
)
from tests.fakes import finding_json


def finding(**overrides) -> Finding:
    return Finding(**finding_json(**overrides))


def state_for(review: Review, **extra) -> dict:
    return {"review": review, "head_sha": "0123456789abcdef", "node_status": {}, **extra}


def review_with(
    *findings: Finding, verdict="comment", summary="Summary text.", omitted=0
) -> Review:
    return Review(
        verdict=verdict, summary=summary, findings=list(findings), omitted_findings=omitted
    )


def test_comment_starts_with_the_hidden_marker_and_shows_verdict_and_summary():
    text = format_comment(state_for(review_with(finding(), verdict="request_changes")))

    assert text.startswith(BOT_MARKER + "\n")
    assert text.count(BOT_MARKER) == 1
    assert "**Verdict:** ❌ Changes requested" in text
    assert "Summary text." in text
    assert "`0123456`" in text  # short commit sha in the footer


def test_findings_are_grouped_by_severity_high_first():
    text = format_comment(
        state_for(
            review_with(
                finding(title="LOW ONE", severity="low"),
                finding(title="HIGH ONE", severity="high"),
                finding(title="MEDIUM ONE", severity="medium"),
            )
        )
    )

    assert (
        text.index("### 🔴 High (1)")
        < text.index("### 🟠 Medium (1)")
        < text.index("### 🟡 Low (1)")
    )
    assert text.index("HIGH ONE") < text.index("MEDIUM ONE") < text.index("LOW ONE")


def test_finding_shows_location_category_confidence_and_suggestion():
    text = format_comment(
        state_for(
            review_with(finding(line_start=10, line_end=14, confidence=0.85, category="security"))
        )
    )

    assert "`src/app.py:10-14`" in text
    assert "security" in text and "85%" in text
    assert "> **Suggestion:** Fix it." in text


def test_single_line_and_file_level_locations():
    single = format_comment(state_for(review_with(finding(line_start=7, line_end=7))))
    general = format_comment(
        state_for(review_with(finding(file_path=None, line_start=None, line_end=None)))
    )

    assert "`src/app.py:7`" in single and "7-7" not in single
    assert "`src/app.py" not in general


def test_test_suggestions_are_collapsible_with_a_fenced_code_block():
    suggestion = finding(
        category="missing_test",
        title="Cover the empty input",
        suggested_test_code="def test_empty():\n    assert f('') == ''",
    )

    text = format_comment(state_for(review_with(suggestion)))

    assert "### 🧪 Suggested tests (1)" in text
    assert "<details>" in text and "</details>" in text
    assert "<summary>Cover the empty input — `src/app.py`</summary>" in text
    assert "```python\ndef test_empty():" in text
    assert "### 🟠 Medium" not in text  # tests are not double-listed as ordinary findings


def test_code_fence_grows_to_contain_backticks_in_the_code():
    code = "print('''```danger```''')"

    block = fenced(code, "python")

    assert block.startswith("````python\n") and block.endswith("\n````")


def test_no_findings_says_so():
    text = format_comment(state_for(review_with(verdict="approve", summary="Clean.")))

    assert "No issues found." in text


def test_notes_explain_truncation_failures_and_skipped_files():
    text = format_comment(
        state_for(
            review_with(finding()),
            context_notes=["2 large file diff(s) were truncated: a.py, b.py"],
            skipped_files={"package-lock.json": "lockfile"},
            chunks=[object(), object()],
            chunks_failed=1,
            node_status={"test_analysis": "failed", "final_review": "degraded"},
        )
    )

    assert "> ⚠️ 2 large file diff(s) were truncated" in text
    assert "1 file(s) were skipped" in text
    assert "1 of 2 parts of the diff could not be reviewed" in text
    assert "Test analysis failed, so this review has no test suggestions." in text
    assert "final ranking step failed" in text


def test_llm_written_text_cannot_inject_html_mentions_or_a_second_marker():
    hostile = finding(
        title="<script>alert(1)</script> @octocat",
        description=f"see {BOT_MARKER} and <img src=x onerror=y> ping @maintainer",
        suggestion="<b>bold</b>",
    )

    text = format_comment(state_for(review_with(hostile, summary="<h1>Hi</h1> @team")))

    assert text.count(BOT_MARKER) == 1  # only ours, at the top
    assert "<script>" not in text and "<img" not in text and "<b>" not in text
    assert "<h1>" not in text
    assert "@octocat" not in text and "@maintainer" not in text and "@team" not in text
    assert "@​octocat" in text


def test_escape_leaves_ordinary_comparisons_and_emails_alone():
    assert escape("if a < b and c > d") == "if a < b and c > d"
    assert escape("mail me: dev@example.com") == "mail me: dev@example.com"


def test_details_tags_stay_balanced():
    findings = [
        finding(category="missing_test", title=f"t{i}", suggested_test_code="assert True")
        for i in range(5)
    ]

    text = format_comment(state_for(review_with(*findings)))

    assert text.count("<details>") == text.count("</details>") == 5


def test_findings_cap_message_is_shown():
    text = format_comment(state_for(review_with(finding(), omitted=3)))

    assert "3 more finding(s) not shown" in text


def huge_review(n: int = 300) -> Review:
    findings = [
        finding(
            title=f"Finding {i}",
            severity=("high", "medium", "low")[i % 3],
            description="d" * 5_000,
            suggestion="s" * 3_000,
        )
        for i in range(n)
    ]
    findings += [
        finding(
            category="missing_test",
            title=f"Test {i}",
            suggested_test_code="t = 1\n" * 5_000,
            description="x" * 5_000,
        )
        for i in range(n)
    ]
    return review_with(*findings)


def test_comment_stays_under_the_github_limit_even_for_a_huge_review():
    text = format_comment(state_for(huge_review()))

    assert len(text) <= SAFE_LIMIT < GITHUB_COMMENT_LIMIT
    assert len(text) > 40_000  # it really is a big comment, not an empty one
    assert "more finding(s) not shown" in text
    assert text.startswith(BOT_MARKER) and "**Verdict:**" in text


def test_size_limit_drops_the_lowest_priority_content_first():
    text = format_comment(state_for(huge_review()))

    assert "Finding 0" in text  # a high-severity finding survives
    assert "Test 299" not in text  # suggested tests come last and are cut first
    assert text.count("<details>") == text.count("</details>")


def test_a_single_enormous_field_cannot_break_the_limit():
    giant = finding(description="z" * 500_000, suggestion="y" * 500_000)
    code = finding(category="missing_test", suggested_test_code="c" * 500_000)

    text = format_comment(state_for(review_with(giant, code, summary="s" * 500_000)))

    assert len(text) <= SAFE_LIMIT
