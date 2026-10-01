"""Agent state and the pydantic schemas exchanged with the LLM.

Categories and severities are `Literal`s that must match the DB check constraints in
`db/models.py`; a test keeps them in sync.
"""

import operator
from typing import Annotated, Any, ClassVar, Literal, TypedDict

from pydantic import BaseModel, Field, field_validator

from reviewer.schemas import ChangedFile

Category = Literal["bug", "security", "performance", "quality", "edge_case", "missing_test"]
Severity = Literal["high", "medium", "low"]
Verdict = Literal["approve", "comment", "request_changes"]
NodeStatus = Literal["ok", "skipped", "degraded", "failed"]

SEVERITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

MAX_TITLE_LENGTH = 255  # DB column size


def _clamp_confidence(value: float) -> float:
    return min(max(float(value), 0.0), 1.0)


class Finding(BaseModel):
    category: Category
    severity: Severity
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    title: str
    description: str
    suggestion: str | None = None
    confidence: float = 0.5
    suggested_test_code: str | None = None  # only set for missing_test findings

    @field_validator("title")
    @classmethod
    def _cap_title(cls, value: str) -> str:
        return value[:MAX_TITLE_LENGTH]

    @field_validator("confidence")
    @classmethod
    def _cap_confidence(cls, value: float) -> float:
        return _clamp_confidence(value)


class TestSuggestion(BaseModel):
    """A missing test found by test_analysis; converted to a `missing_test` Finding at the end."""

    __test__: ClassVar[bool] = False  # not a pytest test class

    title: str
    description: str
    severity: Severity = "medium"
    file_path: str | None = None  # the changed source file this test would cover
    test_file: str | None = None  # where the test should live
    framework: Literal["pytest", "jest", "vitest"] | None = None
    test_code: str | None = None
    confidence: float = 0.5

    @field_validator("title")
    @classmethod
    def _cap_title(cls, value: str) -> str:
        return value[:MAX_TITLE_LENGTH]

    @field_validator("confidence")
    @classmethod
    def _cap_confidence(cls, value: float) -> float:
        return _clamp_confidence(value)

    def to_finding(self) -> Finding:
        where = f" Suggested location: {self.test_file}." if self.test_file else ""
        return Finding(
            category="missing_test",
            severity=self.severity,
            file_path=self.file_path,
            title=self.title,
            description=self.description,
            suggestion=where.strip() or None,
            confidence=self.confidence,
            suggested_test_code=self.test_code,
        )


class Review(BaseModel):
    """The final, post-processed review that gets formatted into the PR comment."""

    verdict: Verdict
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    omitted_findings: int = 0  # dropped by the findings cap


# --- LLM output schemas -----------------------------------------------------------------------


class CodeReviewOutput(BaseModel):
    findings: list[Finding] = Field(default_factory=list)


class TestAnalysisOutput(BaseModel):
    __test__: ClassVar[bool] = False

    suggestions: list[TestSuggestion] = Field(default_factory=list)


class FinalReviewDecision(BaseModel):
    verdict: Verdict
    summary: str
    ranked_ids: list[int] = Field(default_factory=list)  # most important first
    drop_ids: list[int] = Field(default_factory=list)  # duplicates / noise only


# --- graph state ------------------------------------------------------------------------------


class DiffChunk(BaseModel):
    index: int
    files: list[str]
    text: str  # line-numbered diff for these files


def _merge_dicts(left: dict, right: dict) -> dict:
    return {**left, **right}


class ReviewState(TypedDict, total=False):
    # input
    repo: str
    pr_number: int
    head_sha: str
    # fetch_context
    pr_title: str
    pr_body: str
    changed_files: list[ChangedFile]
    reviewed_files: list[str]
    skipped_files: dict[str, str]  # path -> reason
    line_ranges: dict[str, list[tuple[int, int]]]  # path -> new-file line ranges inside hunks
    chunks: list[DiffChunk]
    diff_truncated: bool
    context_notes: list[str]  # truncation and enrichment problems, shown in the comment
    existing_tests: dict[str, str]  # test file path -> content
    js_test_framework: str | None
    # code_review / test_analysis / final_review
    code_findings: list[Finding]
    chunks_failed: int
    test_findings: list[TestSuggestion]
    review: Review | None
    # bookkeeping (reducers: nodes append/merge instead of overwriting)
    node_status: Annotated[dict[str, str], _merge_dicts]
    validation: Annotated[dict[str, dict[str, int]], _merge_dicts]
    errors: Annotated[list[str], operator.add]
    llm_calls: Annotated[list[dict[str, Any]], operator.add]
    fatal_error: str | None
