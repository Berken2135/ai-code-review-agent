import uuid

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from reviewer.config import Settings, get_settings
from reviewer.db.repo import RunRepository
from reviewer.db.session import session_scope
from reviewer.github.signature import verify_signature
from reviewer.schemas import PullRequestEvent
from reviewer.services import review_service

router = APIRouter(tags=["webhooks"])
log = structlog.get_logger(__name__)

HANDLED_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})


def _record_run(event: PullRequestEvent, delivery_id: str) -> uuid.UUID | None:
    """Upsert the PR and queue a run. Returns None if this delivery was already recorded."""
    # Two attempts: a concurrent redelivery can win the insert race (unique constraint); the
    # retry then sees the winner's row and reports a duplicate.
    for attempt in range(2):
        try:
            with session_scope() as session:
                repo = RunRepository(session)
                if repo.get_run_by_delivery_id(delivery_id):
                    return None
                pr = repo.get_or_create_pull_request(
                    event.repository.full_name, event.pull_request.number
                )
                return repo.create_run(pr.id, event.pull_request.head.sha, delivery_id).id
        except IntegrityError:
            if attempt:
                raise
    raise AssertionError("unreachable")


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    secret = settings.github_webhook_secret
    if secret is None:  # fail closed: never accept unsigned traffic
        raise HTTPException(503, "GITHUB_WEBHOOK_SECRET is not configured")

    body = await request.body()
    if not verify_signature(
        secret.get_secret_value(), body, request.headers.get("x-hub-signature-256")
    ):
        raise HTTPException(401, "invalid signature")

    event_type = request.headers.get("x-github-event")
    if event_type == "ping":
        return JSONResponse({"status": "pong"})
    if event_type != "pull_request":
        return JSONResponse({"status": "ignored", "reason": f"event {event_type!r}"})

    delivery_id = request.headers.get("x-github-delivery")
    if not delivery_id:
        raise HTTPException(400, "missing X-GitHub-Delivery header")
    try:
        event = PullRequestEvent.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(400, "invalid pull_request payload") from exc

    if event.action not in HANDLED_ACTIONS:
        return JSONResponse({"status": "ignored", "reason": f"action {event.action!r}"})
    if event.pull_request.draft:
        return JSONResponse({"status": "ignored", "reason": "draft"})

    run_id = await run_in_threadpool(_record_run, event, delivery_id)
    if run_id is None:
        log.info("webhook_duplicate_delivery", delivery_id=delivery_id)
        return JSONResponse({"status": "duplicate"})

    log.info(
        "review_queued",
        run_id=str(run_id),
        repo=event.repository.full_name,
        pr=event.pull_request.number,
        action=event.action,
    )
    background_tasks.add_task(review_service.run, run_id)
    return JSONResponse({"status": "queued", "run_id": str(run_id)}, status_code=202)
