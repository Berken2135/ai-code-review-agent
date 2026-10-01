"""Webhook -> DB flow on SQLite, using fixture payloads. Postgres variant: see test_postgres.py."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from reviewer.config import get_settings
from reviewer.db.models import PullRequest, Run
from reviewer.db.session import session_scope
from reviewer.main import create_app
from reviewer.services import review_service
from tests.fakes import SHA, clean_dependencies
from tests.helpers import WEBHOOK_SECRET, load_fixture, webhook_headers


@pytest.fixture
def client(monkeypatch, db_tables):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    get_settings.cache_clear()  # db_tables already cached settings without the secret
    monkeypatch.setattr(review_service, "build_dependencies", clean_dependencies)
    return TestClient(create_app())


def deliver(client, payload: dict, delivery: str):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/github", content=body, headers=webhook_headers(body, delivery=delivery)
    )


def count(model) -> int:
    with session_scope() as session:
        return session.scalar(select(func.count()).select_from(model))


def test_opened_pr_is_recorded_and_review_scheduled(client):
    response = deliver(client, load_fixture("pr_opened.json"), "delivery-1")

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    with session_scope() as session:
        pr = session.scalar(select(PullRequest))
        run = session.scalar(select(Run))
        assert (pr.repo_full_name, pr.number) == ("octo-org/octo-repo", 42)
        # a finished run records the commit that was actually reviewed (what GitHub served),
        # not the sha from the webhook, which may be stale by the time the review runs
        assert run.head_sha == SHA
        assert run.trigger_delivery_id == "delivery-1"
        # TestClient runs background tasks before returning: the (fake-backed) review has run.
        assert run.status == "succeeded"


def test_redelivery_is_a_noop(client):
    payload = load_fixture("pr_opened.json")

    first = deliver(client, payload, "delivery-1")
    second = deliver(client, payload, "delivery-1")

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate"}
    assert count(Run) == 1


def test_new_push_reuses_pr_row_and_adds_a_run(client):
    opened = load_fixture("pr_opened.json")
    pushed = load_fixture("pr_opened.json") | {"action": "synchronize"}
    pushed["pull_request"]["head"]["sha"] = "f" * 40

    deliver(client, opened, "delivery-1")
    response = deliver(client, pushed, "delivery-2")

    assert response.status_code == 202
    assert count(PullRequest) == 1
    assert count(Run) == 2


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened", "ready_for_review"])
def test_every_handled_action_queues_a_run(client, action):
    payload = load_fixture("pr_opened.json") | {"action": action}

    assert deliver(client, payload, f"delivery-{action}").status_code == 202


def test_draft_and_unhandled_events_create_no_rows(client):
    draft = load_fixture("pr_opened.json")
    draft["pull_request"]["draft"] = True

    deliver(client, draft, "d-draft")
    deliver(client, load_fixture("pr_opened.json") | {"action": "closed"}, "d-closed")

    assert count(Run) == 0
    assert count(PullRequest) == 0
