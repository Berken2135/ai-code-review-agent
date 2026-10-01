"""Review orchestration: claim a run, run the agent, post the comment, persist everything.

Runs as a FastAPI background task. The one rule: no exception may leave `run` unrecorded. Every
failure ends up as `status=failed` plus a readable `error` on the run row (and in the logs), and
LLM calls made before the failure are kept. If even recording fails (DB down), it is logged, and
the startup sweep marks the run failed on the next start.
"""

import uuid
from dataclasses import dataclass
from typing import Protocol

import structlog

from reviewer.agent.graph import ReviewFailedError, run_review
from reviewer.agent.ports import GitHubPort
from reviewer.agent.state import ReviewState
from reviewer.config import Settings, get_settings
from reviewer.db.repo import RunRepository
from reviewer.db.session import session_scope
from reviewer.github.client import GitHubClient
from reviewer.llm.base import LLMClient
from reviewer.llm.factory import create_llm_client
from reviewer.services.comment_formatter import format_comment

log = structlog.get_logger(__name__)

MAX_ERROR_LENGTH = 2_000
MAX_WARNINGS_LENGTH = 4_000


class ReviewGitHub(GitHubPort, Protocol):
    """What the service needs from GitHub: the agent's reads plus posting the comment."""

    def upsert_bot_comment(self, repo: str, number: int, body: str) -> int: ...


@dataclass
class Dependencies:
    github: ReviewGitHub
    llm: LLMClient


@dataclass
class Target:
    repo: str
    number: int
    head_sha: str


@dataclass
class Outcome:
    state: ReviewState | None = None
    comment_id: int | None = None
    error: str | None = None


def build_dependencies(settings: Settings | None = None) -> Dependencies:
    """Real clients from settings. Raises (clear message, no network) if a key is missing."""
    settings = settings or get_settings()
    llm = create_llm_client(settings)  # first: it validates the LLM key before we build GitHub's
    return Dependencies(github=GitHubClient(settings.github_token), llm=llm)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]


def run(run_id: uuid.UUID, deps: Dependencies | None = None) -> None:
    """Background task entry point. Never raises."""
    try:
        _run(run_id, deps)
    except Exception:
        log.exception("review_run_crashed", run_id=str(run_id))
        _record_failure(run_id, "Unexpected internal error while reviewing; see the app logs.")


def _run(run_id: uuid.UUID, deps: Dependencies | None) -> None:
    target = _claim(run_id)
    if target is None:
        return
    log.info("review_run_started", run_id=str(run_id), repo=target.repo, pr=target.number)

    outcome = _execute(target, deps)

    try:
        _persist(run_id, outcome)
    except Exception as exc:
        log.exception("review_run_persist_failed", run_id=str(run_id))
        _record_failure(run_id, f"Review finished but saving the results failed: {_describe(exc)}")
        return
    log.info(
        "review_run_finished",
        run_id=str(run_id),
        status="failed" if outcome.error else "succeeded",
        error=outcome.error,
        llm_calls=len((outcome.state or {}).get("llm_calls", [])),
    )


def _claim(run_id: uuid.UUID) -> Target | None:
    """queued -> running. None (and a log line) if there is nothing to do."""
    with session_scope() as session:
        repo = RunRepository(session)
        if not repo.claim_run(run_id):
            log.warning("review_run_skipped_not_queued", run_id=str(run_id))
            return None
        target = repo.get_run_target(run_id)
    return None if target is None else Target(*target)


def _execute(target: Target, deps: Dependencies | None) -> Outcome:
    """Run the agent and post the comment. Never raises; failures become `outcome.error`."""
    outcome = Outcome()
    owns_clients = deps is None
    try:
        deps = deps or build_dependencies()
        try:
            outcome.state = run_review(
                deps.github,
                deps.llm,
                repo=target.repo,
                pr_number=target.number,
                head_sha=target.head_sha,
            )
        except ReviewFailedError as exc:
            outcome.state, outcome.error = exc.state, str(exc)[:MAX_ERROR_LENGTH]
            return outcome
        try:
            body = format_comment(outcome.state)
            outcome.comment_id = deps.github.upsert_bot_comment(target.repo, target.number, body)
        except Exception as exc:
            outcome.error = f"Review finished but posting the PR comment failed: {_describe(exc)}"
    except Exception as exc:
        outcome.error = _describe(exc)
    finally:
        if owns_clients and deps is not None:
            close = getattr(deps.github, "close", None)
            if close:
                close()
    return outcome


def _warnings(state: ReviewState) -> str | None:
    lines = [*state.get("errors", []), *state.get("context_notes", [])]
    return "\n".join(lines)[:MAX_WARNINGS_LENGTH] or None


def _persist(run_id: uuid.UUID, outcome: Outcome) -> None:
    """One transaction: LLM calls, findings and the final run status."""
    state: ReviewState = outcome.state or {}
    calls = state.get("llm_calls", [])
    review = state.get("review")
    with session_scope() as session:
        repo = RunRepository(session)
        repo.add_llm_calls(run_id, calls)
        if review:
            repo.add_findings(run_id, [finding.model_dump() for finding in review.findings])
        repo.finish_run(
            run_id,
            status="failed" if outcome.error else "succeeded",
            error=outcome.error,
            summary=review.summary if review else None,
            verdict=review.verdict if review else None,
            warnings=_warnings(state),
            head_sha=state.get("head_sha"),
            input_tokens=sum(c["input_tokens"] for c in calls),
            output_tokens=sum(c["output_tokens"] for c in calls),
            latency_ms=sum(c["latency_ms"] for c in calls),
            review_comment_id=outcome.comment_id,
        )


def _record_failure(run_id: uuid.UUID, message: str) -> None:
    """Last resort: mark the run failed. If the DB itself is down, all we can do is log."""
    try:
        with session_scope() as session:
            RunRepository(session).finish_run(run_id, status="failed", error=message)
    except Exception:
        log.exception("review_run_failure_not_recorded", run_id=str(run_id))
