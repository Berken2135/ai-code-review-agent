from typing import get_args

import pytest

from reviewer.agent.state import Category, Finding, Severity, TestSuggestion
from reviewer.agent.validation import (
    clamp_lines,
    resolve_path,
    safe_test_path,
    validate_findings,
    validate_test_suggestions,
)
from reviewer.db.models import FINDING_CATEGORIES, FINDING_SEVERITIES

FILES = {"src/app.py", "src/utils.py", "lib/utils.py", "web/button.ts"}


def make_finding(**overrides) -> Finding:
    data = {
        "category": "bug",
        "severity": "medium",
        "file_path": "src/app.py",
        "line_start": 5,
        "line_end": 6,
        "title": "t",
        "description": "d",
        "confidence": 0.9,
    }
    return Finding(**{**data, **overrides})


def test_schema_literals_match_the_db_check_constraints():
    assert get_args(Category) == FINDING_CATEGORIES
    assert get_args(Severity) == FINDING_SEVERITIES


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/app.py", "src/app.py"),
        ("./src/app.py", "src/app.py"),
        ("b/src/app.py", "src/app.py"),
        ("a/src/app.py", "src/app.py"),
        ("/src/app.py", "src/app.py"),
        ("src\\app.py", "src/app.py"),
        ("app.py", "src/app.py"),  # unambiguous bare name
        ("button.ts", "web/button.ts"),
        ("utils.py", None),  # ambiguous: src/utils.py and lib/utils.py
        ("ghost.py", None),
        ("src/ghost.py", None),
        ("", None),
    ],
)
def test_resolve_path(raw, expected):
    assert resolve_path(raw, FILES) == expected


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (12, 14, (12, 14)),  # inside a hunk: untouched
        (12, 90, (12, 20)),  # end beyond the hunk: clamped to the hunk end
        (12, None, (12, 12)),
        (None, 15, (15, 15)),
        (15, 12, (12, 15)),  # swapped
        (1, 3, (10, 10)),  # before the first hunk: snapped to its first line
        (25, 27, (20, 20)),  # between hunks: snapped to the nearest edge
        (60, 70, (55, 55)),  # after the last hunk: snapped to its last line
        (None, None, (None, None)),
    ],
)
def test_clamp_lines(start, end, expected):
    assert clamp_lines(start, end, [(10, 20), (50, 55)]) == expected


def test_clamp_lines_without_hunks_drops_line_numbers():
    assert clamp_lines(3, 4, []) == (None, None)


def test_hallucinated_paths_are_dropped_and_counted():
    findings = [make_finding(), make_finding(file_path="src/ghost.py", title="ghost")]

    valid, stats = validate_findings(findings, FILES, {"src/app.py": [(1, 20)]})

    assert [f.title for f in valid] == ["t"]
    assert stats["dropped_unknown_file"] == 1


def test_fixable_paths_are_repaired_ambiguous_ones_dropped():
    findings = [
        make_finding(file_path="b/src/app.py"),
        make_finding(file_path="utils.py"),
    ]

    valid, stats = validate_findings(findings, FILES, {"src/app.py": [(1, 20)]})

    assert [f.file_path for f in valid] == ["src/app.py"]
    assert stats["fixed_path"] == 1
    assert stats["dropped_unknown_file"] == 1


def test_line_numbers_are_clamped_to_the_diff_and_counted():
    finding = make_finding(line_start=500, line_end=900)

    (valid,), stats = validate_findings([finding], FILES, {"src/app.py": [(1, 20)]})

    assert (valid.line_start, valid.line_end) == (20, 20)
    assert stats["clamped_lines"] == 1


def test_general_findings_are_kept_but_lose_line_numbers():
    finding = make_finding(file_path=None, line_start=3, line_end=4)

    (valid,), _ = validate_findings([finding], FILES, {})

    assert valid.file_path is None
    assert valid.line_start is None and valid.line_end is None


def test_unknown_test_target_is_dropped_and_unsafe_test_paths_are_cleared():
    suggestions = [
        TestSuggestion(
            title="ok", description="d", file_path="b/src/app.py", test_file="tests/t.py"
        ),
        TestSuggestion(title="ghost", description="d", file_path="nope.py"),
        TestSuggestion(
            title="escape", description="d", file_path="src/app.py", test_file="../../x.py"
        ),
        TestSuggestion(title="abs", description="d", file_path="src/app.py", test_file="/etc/x.py"),
    ]

    valid, stats = validate_test_suggestions(suggestions, FILES)

    assert [s.title for s in valid] == ["ok", "escape", "abs"]
    assert valid[0].file_path == "src/app.py" and valid[0].test_file == "tests/t.py"
    assert valid[1].test_file is None and valid[2].test_file is None
    assert stats == {"dropped_unknown_file": 1, "fixed_path": 1, "dropped_unsafe_test_path": 2}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("tests/test_a.py", "tests/test_a.py"),
        ("../a.py", None),
        ("/a.py", None),
        ("C:/a.py", None),
        (None, None),
    ],
)
def test_safe_test_path(raw, expected):
    assert safe_test_path(raw) == expected


def test_finding_schema_normalises_llm_quirks():
    finding = make_finding(title="x" * 400, confidence=7)

    assert len(finding.title) == 255
    assert finding.confidence == 1.0
