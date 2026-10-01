"""Bearer-token protection of /runs. Fails closed when no token is configured."""

import pytest
from fastapi.testclient import TestClient

from reviewer.config import get_settings
from reviewer.main import create_app
from tests.helpers import AUTH, RUNS_TOKEN, seed_run

PATHS = ["/runs", "/runs/00000000-0000-0000-0000-000000000000"]


@pytest.fixture
def client(db_tables):
    return TestClient(create_app())


@pytest.mark.parametrize("path", PATHS)
def test_missing_token_is_401_with_a_bearer_challenge(client, path):
    response = client.get(path)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    [
        f"Bearer {RUNS_TOKEN}x",
        f"Bearer {RUNS_TOKEN[:-1]}",
        "Bearer ",
        f"Basic {RUNS_TOKEN}",
        RUNS_TOKEN,
        "Bearer ü".encode("latin-1"),  # raw non-ASCII bytes must not crash the check
    ],
)
def test_wrong_token_or_scheme_is_401(client, header):
    assert client.get("/runs", headers={"Authorization": header}).status_code == 401


def test_correct_token_is_accepted(client):
    run_id = seed_run()

    assert client.get("/runs", headers=AUTH).status_code == 200
    assert client.get(f"/runs/{run_id}", headers=AUTH).status_code == 200


def test_scheme_is_case_insensitive(client):
    assert client.get("/runs", headers={"Authorization": f"bearer {RUNS_TOKEN}"}).status_code == 200


@pytest.mark.parametrize("value", [None, ""])
@pytest.mark.parametrize("path", PATHS)
def test_unconfigured_token_fails_closed(monkeypatch, db_tables, path, value):
    if value is None:
        monkeypatch.delenv("RUNS_API_TOKEN")
    else:
        monkeypatch.setenv("RUNS_API_TOKEN", value)  # what `RUNS_API_TOKEN=` in .env produces
    get_settings.cache_clear()

    response = TestClient(create_app()).get(path, headers=AUTH)

    assert response.status_code == 503
    assert "RUNS_API_TOKEN" in response.json()["detail"]


def test_health_webhook_and_docs_are_not_behind_the_token(client):
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    # the webhook has its own protection (HMAC signature), not the bearer token
    assert client.post("/webhooks/github", content=b"{}").status_code in (401, 503)


def test_token_never_appears_in_the_error_response(client):
    response = client.get("/runs", headers={"Authorization": "Bearer wrong"})

    assert RUNS_TOKEN not in response.text
