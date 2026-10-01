import copy
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"
WEBHOOK_SECRET = "test-webhook-secret"
RUNS_TOKEN = "test-runs-token"
AUTH = {"Authorization": f"Bearer {RUNS_TOKEN}"}


def load_fixture(name: str) -> dict[str, Any]:
    return copy.deepcopy(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def webhook_headers(
    body: bytes, event: str = "pull_request", delivery: str | None = "delivery-1"
) -> dict[str, str]:
    headers = {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": sign(body),
        "Content-Type": "application/json",
    }
    if delivery is not None:
        headers["X-GitHub-Delivery"] = delivery
    return headers


def seed_run(
    *,
    repo: str = "octo-org/orders",
    number: int = 17,
    status: str = "queued",
    created_at=None,
    findings: list[dict] | None = None,
    calls: list[dict] | None = None,
    **fields: Any,
):
    """Insert a run (and optional findings / llm_calls rows) directly. Returns its id."""
    import uuid

    from reviewer.db.models import Run
    from reviewer.db.repo import RunRepository
    from reviewer.db.session import session_scope

    with session_scope() as session:
        repository = RunRepository(session)
        pr = repository.get_or_create_pull_request(repo, number)
        extra = {"created_at": created_at} if created_at else {}
        run = Run(
            id=uuid.uuid4(),
            pr_id=pr.id,
            head_sha="a" * 40,
            status=status,
            trigger_delivery_id=str(uuid.uuid4()),
            **extra,
            **fields,
        )
        session.add(run)
        session.flush()
        repository.add_findings(run.id, findings or [])
        repository.add_llm_calls(run.id, calls or [])
        return run.id


def finding_row(**overrides: Any) -> dict:
    row = {
        "category": "bug",
        "severity": "medium",
        "file_path": "src/app.py",
        "line_start": 3,
        "line_end": 4,
        "title": "A finding",
        "description": "Details.",
        "suggestion": "Fix it.",
        "suggested_test_code": None,
        "confidence": 0.9,
    }
    return {**row, **overrides}


def call_row(**overrides: Any) -> dict:
    row = {
        "node": "code_review",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "input_tokens": 100,
        "output_tokens": 20,
        "latency_ms": 500,
        "retries": 0,
        "error": None,
    }
    return {**row, **overrides}
