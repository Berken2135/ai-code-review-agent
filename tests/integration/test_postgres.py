"""Needs a real Postgres. Skipped unless POSTGRES_TEST_URL is set, e.g.

    POSTGRES_TEST_URL=postgresql+psycopg://user:pass@localhost:5432/testdb pytest -m integration

The database is migrated to head for the test and downgraded to base afterwards.
"""

import json
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from reviewer.main import create_app
from reviewer.services import review_service
from tests.fakes import clean_dependencies
from tests.helpers import WEBHOOK_SECRET, load_fixture, webhook_headers

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("POSTGRES_TEST_URL"), reason="POSTGRES_TEST_URL not set"),
]

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def test_migrations_and_webhook_flow_on_postgres(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["POSTGRES_TEST_URL"])
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr(review_service, "build_dependencies", clean_dependencies)
    from reviewer.config import get_settings

    get_settings.cache_clear()
    alembic_cfg = Config(str(ALEMBIC_INI))
    command.upgrade(alembic_cfg, "head")
    try:
        client = TestClient(create_app())
        body = json.dumps(load_fixture("pr_opened.json")).encode()
        headers = webhook_headers(body, delivery=f"pg-{uuid.uuid4()}")

        first = client.post("/webhooks/github", content=body, headers=headers)
        second = client.post("/webhooks/github", content=body, headers=headers)

        assert first.status_code == 202
        assert second.status_code == 200
        assert second.json() == {"status": "duplicate"}
    finally:
        command.downgrade(alembic_cfg, "base")
