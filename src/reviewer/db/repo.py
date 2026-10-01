"""All database queries live here. No business logic; the caller owns the transaction."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, contains_eager, selectinload

from reviewer.db.models import Finding, LLMCall, PullRequest, Run


class RunRepository:
    def __init__(self, session: Session):
        self.session = session

    # --- webhook side ----------------------------------------------------------------------

    def get_or_create_pull_request(self, repo_full_name: str, number: int) -> PullRequest:
        pr = self.session.scalar(
            select(PullRequest).where(
                PullRequest.repo_full_name == repo_full_name, PullRequest.number == number
            )
        )
        if pr is None:
            pr = PullRequest(repo_full_name=repo_full_name, number=number)
            self.session.add(pr)
            self.session.flush()
        return pr

    def get_run(self, run_id: uuid.UUID) -> Run | None:
        return self.session.get(Run, run_id)

    def get_run_by_delivery_id(self, delivery_id: str) -> Run | None:
        return self.session.scalar(select(Run).where(Run.trigger_delivery_id == delivery_id))

    def create_run(self, pr_id: int, head_sha: str, delivery_id: str) -> Run:
        run = Run(
            id=uuid.uuid4(),
            pr_id=pr_id,
            head_sha=head_sha,
            status="queued",
            trigger_delivery_id=delivery_id,
        )
        self.session.add(run)
        self.session.flush()
        return run

    # --- run lifecycle ---------------------------------------------------------------------

    def claim_run(self, run_id: uuid.UUID) -> bool:
        """queued -> running, atomically. False if the run is missing or not queued."""
        result = self.session.execute(
            update(Run)
            .where(Run.id == run_id, Run.status == "queued")
            .values(status="running", started_at=datetime.now(UTC))
        )
        return result.rowcount == 1

    def get_run_target(self, run_id: uuid.UUID) -> tuple[str, int, str] | None:
        """(repo_full_name, pr number, head sha) of a run."""
        row = self.session.execute(
            select(PullRequest.repo_full_name, PullRequest.number, Run.head_sha)
            .join(Run, Run.pr_id == PullRequest.id)
            .where(Run.id == run_id)
        ).one_or_none()
        return None if row is None else (row[0], row[1], row[2])

    def add_llm_calls(self, run_id: uuid.UUID, rows: list[dict[str, Any]]) -> None:
        self.session.add_all(LLMCall(run_id=run_id, **row) for row in rows)

    def add_findings(self, run_id: uuid.UUID, rows: list[dict[str, Any]]) -> None:
        self.session.add_all(Finding(run_id=run_id, **row) for row in rows)

    def finish_run(
        self,
        run_id: uuid.UUID,
        *,
        status: str,
        error: str | None = None,
        summary: str | None = None,
        verdict: str | None = None,
        warnings: str | None = None,
        head_sha: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int = 0,
        review_comment_id: int | None = None,
    ) -> None:
        run = self.session.get(Run, run_id)
        if run is None:
            raise LookupError(f"run {run_id} not found")
        now = datetime.now(UTC)
        run.status = status
        run.error = error
        run.summary = summary
        run.verdict = verdict
        run.warnings = warnings
        run.total_input_tokens = input_tokens
        run.total_output_tokens = output_tokens
        run.total_latency_ms = latency_ms
        run.review_comment_id = review_comment_id
        run.head_sha = head_sha or run.head_sha  # the commit that was actually reviewed
        run.started_at = run.started_at or now
        run.finished_at = now

    def fail_stuck_runs(self, reason: str) -> int:
        """Mark every queued/running run as failed. Returns how many were changed."""
        stuck = self.session.scalars(select(Run).where(Run.status.in_(("queued", "running"))))
        count = 0
        now = datetime.now(UTC)
        for run in stuck:
            run.error = f"{reason} (status was '{run.status}')"
            run.status = "failed"
            run.started_at = run.started_at or now
            run.finished_at = now
            count += 1
        return count

    # --- read side (API) -------------------------------------------------------------------

    def list_runs(
        self,
        *,
        status: str | None = None,
        repo: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[tuple[Run, int]], int]:
        """Newest first. Returns ([(run, findings_count)], total matching runs)."""
        conditions = []
        if status:
            conditions.append(Run.status == status)
        if repo:
            conditions.append(PullRequest.repo_full_name == repo)
        findings_count = (
            select(func.count(Finding.id))
            .where(Finding.run_id == Run.id)
            .correlate(Run)
            .scalar_subquery()
        )
        rows = self.session.execute(
            select(Run, findings_count)
            .join(Run.pull_request)
            .options(contains_eager(Run.pull_request))
            .where(*conditions)
            .order_by(Run.created_at.desc(), Run.id)
            .limit(limit)
            .offset(offset)
        )
        total = self.session.scalar(
            select(func.count()).select_from(Run).join(Run.pull_request).where(*conditions)
        )
        return [(run, count) for run, count in rows], total or 0

    def get_run_detail(self, run_id: uuid.UUID) -> Run | None:
        return self.session.scalar(
            select(Run)
            .where(Run.id == run_id)
            .options(
                selectinload(Run.pull_request),
                selectinload(Run.findings),
                selectinload(Run.llm_calls),
            )
        )
