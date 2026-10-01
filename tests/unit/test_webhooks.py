"""Webhook paths that must never touch the database (no tables exist in these tests)."""

import json

import pytest
from fastapi.testclient import TestClient

from reviewer.main import create_app
from tests.helpers import WEBHOOK_SECRET, load_fixture, webhook_headers


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    return TestClient(create_app())


def post(client, payload: dict | bytes, **header_kwargs):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return client.post(
        "/webhooks/github", content=body, headers=webhook_headers(body, **header_kwargs)
    )


def test_invalid_signature_is_401(client):
    body = json.dumps(load_fixture("pr_opened.json")).encode()
    headers = webhook_headers(body) | {"X-Hub-Signature-256": "sha256=" + "0" * 64}

    assert client.post("/webhooks/github", content=body, headers=headers).status_code == 401


def test_missing_signature_is_401(client):
    body = json.dumps(load_fixture("pr_opened.json")).encode()
    headers = webhook_headers(body)
    del headers["X-Hub-Signature-256"]

    assert client.post("/webhooks/github", content=body, headers=headers).status_code == 401


def test_unconfigured_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    body = json.dumps(load_fixture("pr_opened.json")).encode()

    response = TestClient(create_app()).post(
        "/webhooks/github", content=body, headers=webhook_headers(body)
    )

    assert response.status_code == 503


def test_ping_returns_200(client):
    response = post(client, load_fixture("ping.json"), event="ping")

    assert response.status_code == 200
    assert response.json() == {"status": "pong"}


def test_other_event_types_are_ignored(client):
    response = post(client, {"ref": "refs/heads/main"}, event="push")

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


@pytest.mark.parametrize("action", ["closed", "edited", "labeled", "assigned"])
def test_unhandled_pr_actions_are_ignored(client, action):
    payload = load_fixture("pr_opened.json") | {"action": action}

    response = post(client, payload)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_draft_pr_is_skipped(client):
    payload = load_fixture("pr_opened.json")
    payload["pull_request"]["draft"] = True

    response = post(client, payload)

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "draft"}


def test_invalid_json_is_400(client):
    assert post(client, b"not json").status_code == 400


def test_payload_missing_required_fields_is_400(client):
    assert post(client, {"action": "opened"}).status_code == 400


def test_missing_delivery_header_is_400(client):
    assert post(client, load_fixture("pr_opened.json"), delivery=None).status_code == 400
