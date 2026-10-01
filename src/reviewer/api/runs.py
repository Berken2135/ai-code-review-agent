"""Read-only observability endpoints: what the agent did, what it found, what it cost.

Protected by a static bearer token (RUNS_API_TOKEN). Without one configured the endpoints answer
503 instead of running open, so exposing the app (e.g. through a tunnel) never leaks review data.
"""

import hmac
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from reviewer.agent.state import SEVERITY_RANK
from reviewer.config import Settings, get_settings
from reviewer.db.models import Finding, LLMCall, Run
from reviewer.db.repo import RunRepository
from reviewer.db.session import get_session

_bearer = HTTPBearer(auto_error=False)


def require_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    expected = settings.runs_api_token.get_secret_value() if settings.runs_api_token else ""
    if not expected:
        raise HTTPException(503, "RUNS_API_TOKEN is not configured")
    given = credentials.credentials if credentials else ""
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(
            401, "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"}
        )


router = APIRouter(prefix="/runs", tags=["runs"], dependencies=[Depends(require_token)])

RunStatus = Literal["queued", "running", "succeeded", "failed"]


class TokenUsage(BaseModel):
    input: int
    output: int
    total: int


class FindingOut(BaseModel):
    id: int
    category: str
    severity: str
    file_path: str | None
    line_start: int | None
    line_end: int | None
    title: str
    description: str
    suggestion: str | None
    confidence: float | None
    suggested_test_code: str | None


class LLMCallOut(BaseModel):
    node: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    retries: int
    error: str | None
    created_at: datetime | None


class RunSummary(BaseModel):
    id: uuid.UUID
    status: RunStatus
    repo: str
    pr_number: int
    head_sha: str
    verdict: str | None
    created_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None  # wall clock, started -> finished
    tokens: TokenUsage
    llm_latency_ms: int  # summed over LLM calls
    findings_count: int


class RunDetail(RunSummary):
    summary: str | None
    error: str | None
    warnings: list[str]
    review_comment_id: int | None
    started_at: datetime | None
    findings: list[FindingOut]
    test_suggestions: list[FindingOut]
    llm_calls: list[LLMCallOut]


class RunPage(BaseModel):
    items: list[RunSummary]
    total: int
    limit: int
    offset: int


def _summary(run: Run, findings_count: int) -> dict:
    started, finished = run.started_at, run.finished_at
    return {
        "id": run.id,
        "status": run.status,
        "repo": run.pull_request.repo_full_name,
        "pr_number": run.pull_request.number,
        "head_sha": run.head_sha,
        "verdict": run.verdict,
        "created_at": run.created_at,
        "finished_at": finished,
        "duration_ms": round((finished - started).total_seconds() * 1000)
        if started and finished
        else None,
        "tokens": TokenUsage(
            input=run.total_input_tokens,
            output=run.total_output_tokens,
            total=run.total_input_tokens + run.total_output_tokens,
        ),
        "llm_latency_ms": run.total_latency_ms,
        "findings_count": findings_count,
    }


def _finding(row: Finding) -> FindingOut:
    return FindingOut(
        id=row.id,
        category=row.category,
        severity=row.severity,
        file_path=row.file_path,
        line_start=row.line_start,
        line_end=row.line_end,
        title=row.title,
        description=row.description,
        suggestion=row.suggestion,
        confidence=row.confidence,
        suggested_test_code=row.suggested_test_code,
    )


def _llm_call(row: LLMCall) -> LLMCallOut:
    return LLMCallOut(
        node=row.node,
        provider=row.provider,
        model=row.model,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        latency_ms=row.latency_ms,
        retries=row.retries,
        error=row.error,
        created_at=row.created_at,
    )


@router.get("", response_model=RunPage)
def list_runs(
    status: RunStatus | None = None,
    repo: str | None = Query(None, description="Exact match, e.g. owner/name"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> RunPage:
    rows, total = RunRepository(session).list_runs(
        status=status, repo=repo, limit=limit, offset=offset
    )
    return RunPage(
        items=[RunSummary(**_summary(run, count)) for run, count in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{run_id}", response_model=RunDetail)
def get_run(run_id: uuid.UUID, session: Session = Depends(get_session)) -> RunDetail:
    run = RunRepository(session).get_run_detail(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    findings = sorted(run.findings, key=lambda f: (SEVERITY_RANK[f.severity], f.id))
    return RunDetail(
        **_summary(run, len(findings)),
        summary=run.summary,
        error=run.error,
        warnings=(run.warnings or "").splitlines(),
        review_comment_id=run.review_comment_id,
        started_at=run.started_at,
        findings=[_finding(f) for f in findings if f.category != "missing_test"],
        test_suggestions=[_finding(f) for f in findings if f.category == "missing_test"],
        llm_calls=[_llm_call(c) for c in sorted(run.llm_calls, key=lambda c: c.id)],
    )
